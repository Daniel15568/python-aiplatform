# -*- coding: utf-8 -*-
# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import json
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

if TYPE_CHECKING:
    try:
        from langchain_core import runnables
        from langchain_core import tools as lc_tools

        BaseTool = lc_tools.BaseTool
        GetSessionHistoryCallable = runnables.history.GetSessionHistoryCallable
        RunnableConfig = runnables.RunnableConfig
        RunnableSerializable = runnables.RunnableSerializable
    except ImportError:
        BaseTool = Any
        GetSessionHistoryCallable = Any
        RunnableConfig = Any
        RunnableSerializable = Any


def _default_runnable_kwargs(has_history: bool) -> Mapping[str, Any]:
    # https://github.com/langchain-ai/langchain/blob/5784dfed001730530637793bea1795d9d5a7c244/libs/core/langchain_core/runnables/history.py#L237-L241
    runnable_kwargs = {
        # input_messages_key (str): Must be specified if the underlying
        # agent accepts a dict as input.
        "input_messages_key": "input",
        # output_messages_key (str): Must be specified if the underlying
        # agent returns a dict as output.
        "output_messages_key": "output",
    }
    if has_history:
        # history_messages_key (str): Must be specified if the underlying
        # agent accepts a dict as input and a separate key for historical
        # messages.
        runnable_kwargs["history_messages_key"] = "history"
    return runnable_kwargs


def _default_output_parser():
    from langchain_core import agents
    from langchain_core import output_parsers
    from langchain_core import outputs

    class DefaultOutputParser(output_parsers.BaseOutputParser):

        def parse_result(
            self,
            result: List[outputs.Generation],
        ) -> Union[agents.AgentAction, agents.AgentFinish]:
            if not isinstance(result[0], outputs.ChatGeneration):
                raise ValueError(
                    "This output parser only works on ChatGeneration output"
                )
            msg = result[0].message
            content = msg.content
            function_call = msg.additional_kwargs.get("function_call", {})
            if function_call:
                function_name = function_call["name"]
                tool_input = json.loads(function_call.get("arguments", {}))
                content_msg = f"responded: {content}\n" if content else "\n"
                log_msg = (
                    f"\nInvoking: `{function_name}` with `{tool_input}`\n"
                    f"{content_msg}\n"
                )
                return agents.AgentActionMessageLog(
                    tool=function_name,
                    tool_input=tool_input,
                    log=log_msg,
                    message_log=[msg],
                )
            return agents.AgentFinish(
                return_values={"output": content},
                log=str(content),
            )

        def parse(
            self,
            text: str,
        ) -> Union[agents.AgentAction, agents.AgentFinish]:
            raise ValueError("Can only parse messages")

    return DefaultOutputParser()


def _default_prompt(has_history: bool) -> "RunnableSerializable":
    from langchain_core import agents
    from langchain_core import messages
    from langchain_core import prompts

    def _convert_agent_action_to_messages(
        agent_action: agents.AgentAction, observation: str
    ) -> List[messages.BaseMessage]:
        """Convert an agent action to a message.

        This is used to reconstruct the original message from the agent action.

        Args:
            agent_action (AgentAction): The action to convert into messages.
            observation (str): The observation to convert into messages.

        Returns:
            List[messages.BaseMessage]: A list of messages that corresponds to
            the original tool invocation.
        """
        if isinstance(agent_action, agents.AgentActionMessageLog):
            return list(agent_action.message_log) + [
                _create_function_message(agent_action, observation)
            ]
        else:
            return [messages.AIMessage(content=agent_action.log)]

    def _create_function_message(
        agent_action: agents.AgentAction, observation: str
    ) -> messages.FunctionMessage:
        """Convert agent action and observation into a function message.

        Args:
            agent_action (AgentAction): tool invocation request from the agent.
            observation (str): the result of the tool invocation.

        Returns:
            FunctionMessage: A message corresponding to the tool invocation.
        """
        if not isinstance(observation, str):
            try:
                content = json.dumps(observation, ensure_ascii=False)
            except Exception:
                content = str(observation)
        else:
            content = observation
        return messages.FunctionMessage(name=agent_action.tool, content=content)

    def _format_to_messages(
        intermediate_steps: Sequence[Tuple[agents.AgentAction, str]],
    ) -> List[messages.BaseMessage]:
        """Convert (AgentAction, tool output) tuples into messages.

        Args:
            intermediate_steps (Sequence[Tuple[AgentAction, str]]):
                Required. Steps the model has taken, along with observations.

        Returns:
            List[langchain_core.messages.BaseMessage]: list of messages to send
            to the model for the next generation.

        """
        scratchpad_messages = []
        for agent_action, observation in intermediate_steps:
            scratchpad_messages.extend(
                _convert_agent_action_to_messages(agent_action, observation)
            )
        return scratchpad_messages

    if has_history:
        return {
            "history": lambda x: x["history"],
            "input": lambda x: x["input"],
            "agent_scratchpad": (
                lambda x: _format_to_messages(x["intermediate_steps"])
            ),
        } | prompts.ChatPromptTemplate.from_messages([
            prompts.MessagesPlaceholder(variable_name="history"),
            ("user", "{input}"),
            prompts.MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
    else:
        return {
            "input": lambda x: x["input"],
            "agent_scratchpad": (
                lambda x: _format_to_messages(x["intermediate_steps"])
            ),
        } | prompts.ChatPromptTemplate.from_messages([
            ("user", "{input}"),
            prompts.MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])


def _validate_callable_parameters_are_annotated(callable: Callable):
    """Validates that the parameters of the callable have type annotations.

    This ensures that they can be used for constructing LangChain tools that are
    usable with Gemini function calling.
    """
    import inspect
    parameters = dict(inspect.signature(callable).parameters)
    for name, parameter in parameters.items():
        if parameter.annotation == inspect.Parameter.empty:
            raise TypeError(
                f"Callable={callable.__name__} has untyped input_arg={name}. "
                f"Please specify a type when defining it, e.g. `{name}: str`."
            )


def _convert_tools_or_raise(
    tools: Sequence[Union[Callable, "BaseTool"]]
) -> Sequence["BaseTool"]:
    """Converts the tools into Langchain tools (if needed).

    See https://blog.langchain.dev/structured-tools/ for details.
    """
    from langchain_core import tools as lc_tools
    from langchain.tools.base import StructuredTool
    result = []
    for tool in tools:
        if not isinstance(tool, lc_tools.BaseTool):
            _validate_callable_parameters_are_annotated(tool)
            tool = StructuredTool.from_function(tool)
        result.append(tool)
    return result


class LangchainAgent:
    """A Langchain Agent.

    Reference:
    *   Agent: https://python.langchain.com/docs/modules/agents/concepts
    *   Memory: https://python.langchain.com/docs/expression_language/how_to/message_history
    """

    def __init__(
        self,
        model: str,
        *,
        prompt: Optional["RunnableSerializable"] = None,
        tools: Optional[Sequence[Union[Callable, "BaseTool"]]] = None,
        output_parser: Optional["RunnableSerializable"] = None,
        chat_history: Optional["GetSessionHistoryCallable"] = None,
        model_kwargs: Optional[Mapping[str, Any]] = None,
        agent_executor_kwargs: Optional[Mapping[str, Any]] = None,
        runnable_kwargs: Optional[Mapping[str, Any]] = None,
    ):
        """Initializes the LangchainAgent.

        Under-the-hood, assuming .set_up() is called, this will correspond to

        ```
        from langchain import agents
        from langchain_core.runnables.history import RunnableWithMessageHistory
        from langchain_google_vertexai import ChatVertexAI

        llm = ChatVertexAI(model_name=model, **model_kwargs)
        agent_executor = agents.AgentExecutor(
            agent=prompt | llm.bind(functions=tools) | output_parser,
            tools=tools,
            **agent_executor_kwargs,
        )
        runnable = RunnableWithMessageHistory(
            runnable=agent_executor,
            get_session_history=chat_history,
            **runnable_kwargs,
        )
        ```

        Args:
            model (str):
                Optional. The name of the model (e.g. "gemini-1.0-pro").
            prompt (langchain_core.runnables.RunnableSerializable):
                Optional. The prompt template for the model. Defaults to a
                ChatPromptTemplate.
            tools (Sequence[langchain_core.tools.BaseTool, Callable]):
                Optional. The tools for the agent to be able to use. All input
                callables (e.g. function or class method) will be converted
                to a langchain.tools.base.StructuredTool. Defaults to None.
            output_parser (langchain_core.runnables.RunnableSerializable):
                Optional. The output parser for the model. Defaults to an
                output parser that works with Gemini function-calling.
            chat_history (langchain_core.runnables.history.GetSessionHistoryCallable):
                Optional. Callable that returns a new BaseChatMessageHistory.
                Defaults to None, i.e. chat_history is not preserved.
            model_kwargs (Mapping[str, Any]):
                Optional. Additional keyword arguments for the constructor of
                chat_models.ChatVertexAI. An example would be
                ```
                {
                    # temperature (float): Sampling temperature, it controls the
                    # degree of randomness in token selection.
                    "temperature": 0.28,
                    # max_output_tokens (int): Token limit determines the
                    # maximum amount of text output from one prompt.
                    "max_output_tokens": 1000,
                    # top_p (float): Tokens are selected from most probable to
                    # least, until the sum of their probabilities equals the
                    # top_p value.
                    "top_p": 0.95,
                    # top_k (int): How the model selects tokens for output, the
                    # next token is selected from among the top_k most probable
                    # tokens.
                    "top_k": 40,
                }
                ```
            agent_executor_kwargs (Mapping[str, Any]):
                Optional. Additional keyword arguments for the constructor of
                langchain.agents.AgentExecutor. An example would be
                ```
                {
                    # Whether to return the agent's trajectory of intermediate
                    # steps at the end in addition to the final output.
                    "return_intermediate_steps": False,
                    # The maximum number of steps to take before ending the
                    # execution loop.
                    "max_iterations": 15,
                    # The method to use for early stopping if the agent never
                    # returns `AgentFinish`. Either 'force' or 'generate'.
                    "early_stopping_method": "force",
                    # How to handle errors raised by the agent's output parser.
                    # Defaults to `False`, which raises the error.
                    "handle_parsing_errors": False,
                }
                ```
            runnable_kwargs (Mapping[str, Any]):
                Optional. Additional keyword arguments for the constructor of
                langchain.runnables.history.RunnableWithMessageHistory if
                chat_history is specified. If chat_history is None, this will be
                ignored.

        Raises:
            TypeError: If there is an invalid tool (e.g. function with an input
            that did not specify its type).
        """
        from google.cloud.aiplatform import initializer
        self._project = initializer.global_config.project
        self._location = initializer.global_config.location
        self._tools = []
        if tools:
            # Unlike the other fields, we convert tools at initialization to
            # validate the functions/tools before they are deployed.
            self._tools = _convert_tools_or_raise(tools)
        self._model_name = model
        self._prompt = prompt
        self._output_parser = output_parser
        self._chat_history = chat_history
        self._model_kwargs = model_kwargs
        self._agent_executor_kwargs = agent_executor_kwargs
        self._runnable_kwargs = runnable_kwargs
        self._runnable = None
        self._chat_history_store = None

    def set_up(self):
        """Sets up the agent for execution of queries at runtime.

        It initializes the model, binds the model with tools, and connects it
        with the prompt template and output parser.

        This method should not be called for an object that being passed to
        the ReasoningEngine service for deployment, as it initializes clients
        that can not be serialized.
        """
        from langchain.agents import AgentExecutor
        from langchain_core.runnables.history import RunnableWithMessageHistory
        from langchain_google_vertexai import ChatVertexAI
        import vertexai
        from google.cloud.aiplatform import initializer

        has_history = self._chat_history is not None
        self._prompt = self._prompt or _default_prompt(has_history)
        self._output_parser = self._output_parser or _default_output_parser()
        self._model_kwargs = self._model_kwargs or {}
        self._agent_executor_kwargs = self._agent_executor_kwargs or {}
        self._runnable_kwargs = (
            self._runnable_kwargs or _default_runnable_kwargs(has_history)
        )

        current_project = initializer.global_config.project
        current_location = initializer.global_config.location
        vertexai.init(project=self._project, location=self._location)
        self._llm = ChatVertexAI(
            model_name=self._model_name,
            **self._model_kwargs,
        )
        vertexai.init(project=current_project, location=current_location)

        if self._tools:
            self._llm = self._llm.bind(functions=self._tools)
        self._agent = self._prompt | self._llm | self._output_parser
        self._agent_executor = AgentExecutor(
            agent=self._agent,
            tools=self._tools,
            **self._agent_executor_kwargs,
        )
        runnable = self._agent_executor
        if has_history:
            runnable = RunnableWithMessageHistory(
                runnable=self._agent_executor,
                get_session_history=self._chat_history,
                **self._runnable_kwargs,
            )
        self._runnable = runnable

    def query(
        self,
        *,
        input: Union[str, Mapping[str, Any]],
        config: Optional["RunnableConfig"] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Queries the Agent with the given input and config.

        Args:
            input (Union[str, Mapping[str, Any]]):
                Required. The input to be passed to the Agent.
            config (langchain_core.runnables.RunnableConfig):
                Optional. The config (if any) to be used for invoking the Agent.
            **kwargs:
                Optional. Any additional keyword arguments to be passed to the
                `.invoke()` method of the corresponding AgentExecutor.

        Returns:
            The output of querying the Agent with the given input and config.
        """
        from langchain.load import dump as langchain_load_dump
        if isinstance(input, str):
            input = {"input": input}
        if not self._runnable:
            self.set_up()
        return langchain_load_dump.dumpd(
            self._runnable.invoke(input=input, config=config, **kwargs)
        )
