# -*- coding: utf-8 -*-

# Copyright 2022 Google LLC
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

from collections import defaultdict
from typing import Dict, Union, Optional, Any

from google.api_core import exceptions
from google.auth import credentials as auth_credentials
from google.protobuf import timestamp_pb2

from google.cloud.aiplatform import base
from google.cloud.aiplatform import gapic
from google.cloud.aiplatform import pipeline_jobs
from google.cloud.aiplatform.compat.types import event as gca_event
from google.cloud.aiplatform.compat.types import execution as gca_execution
from google.cloud.aiplatform.metadata import artifact
from google.cloud.aiplatform.metadata import constants
from google.cloud.aiplatform.metadata import context
from google.cloud.aiplatform.metadata import execution
from google.cloud.aiplatform.metadata import experiment_resources
from google.cloud.aiplatform.metadata import experiment_run_resource
from google.cloud.aiplatform.metadata import metadata_store
from google.cloud.aiplatform.tensorboard import tensorboard_resource

_LOGGER = base.Logger(__name__)


def _get_experiment_schema_version() -> str:
    """Helper method to get experiment schema version

    Returns:
        str: schema version of the currently set experiment tracking version
    """
    return constants.SCHEMA_VERSIONS[constants.SYSTEM_EXPERIMENT]


# Legacy Experiment tracking
class _MetadataService:
    """Contains the exposed APIs to interact with the Managed Metadata Service."""

    def __init__(self):
        self._experiment = None
        self._run = None
        self._metrics = None

    def reset(self):
        """Reset all _MetadataService fields to None"""
        self._experiment = None
        self._run = None
        self._metrics = None

    @property
    def experiment_name(self) -> Optional[str]:
        """Return the experiment name of the _MetadataService, if experiment is not set, return None"""
        if self._experiment:
            return self._experiment.display_name
        return None

    @property
    def run_name(self) -> Optional[str]:
        """Return the run name of the _MetadataService, if run is not set, return None"""
        if self._run:
            return self._run.display_name
        return None

    def set_experiment(self, experiment: str, description: Optional[str] = None):
        """Setup a experiment to current session.

        Args:
            experiment (str):
                Required. Name of the experiment to assign current session with.
            description (str):
                Optional. Description of an experiment.
        Raises:
            ValueError:
                If Context with same name as experiment has already been created with
                a different type.

        """

        metadata_store._MetadataStore.get_or_create()

        self.reset()

        experiment_context = context._Context.get_or_create(
            resource_id=experiment,
            display_name=experiment,
            description=description,
            schema_title=constants.SYSTEM_EXPERIMENT,
            schema_version=_get_experiment_schema_version(),
            metadata=constants.EXPERIMENT_METADATA,
        )

        if experiment_context.schema_title != constants.SYSTEM_EXPERIMENT:
            raise ValueError(
                f"Experiment name {experiment} has been used to create other type of resources "
                f"({experiment_context.schema_title}) in this MetadataStore, please choose a different experiment name."
            )

        if description and experiment_context.description != description:
            experiment_context.update(metadata=experiment_context.metadata, description=description)

        self._experiment = experiment_context

    def _create_experiment_run_context(self, run: str) -> context._Context:
        """Creates an ExperimentRun Context and assigns it as a current Experiment.

        Args:
            run (str): The name of the experiment run.
        Returns:
            _Context: The Context representing this ExperimentRun
        Raises:
            ValueError:
                If name of experiment has already been used in Metadata Store to create another
                Context.
        """
        run_context_id = f"{self._experiment.name}-{run}"

        run_context = context._Context.get_or_create(
            resource_id=run_context_id,
            display_name=run,
            schema_title=constants.SYSTEM_EXPERIMENT_RUN,
            schema_version=constants.SCHEMA_VERSIONS[constants.SYSTEM_EXPERIMENT_RUN],
            metadata=constants.EXPERIMENT_METADATA,
        )

        if run_context.schema_title != constants.SYSTEM_EXPERIMENT_RUN:
            raise ValueError(
                f"Run name {run} has been used to create other type of resources ({run_context.schema_title}) "
                "in this MetadataStore, please choose a different run name."
            )

        if self._experiment.resource_name not in run_context.parent_contexts:
            self._experiment.add_context_children([run_context])
            run_context._sync_gca_resource()

        return run_context

    # TODO(b/211012711) add support for resuming runs
    # TODO(b/211013314) add support for returning context manager
    def start_run(self, run: str):
        """Setup a run to current session.

        Args:
            run (str):
                Required. Name of the run to assign current session with.
        Raises:
            ValueError:
                if experiment is not set. Or if run execution or metrics artifact is already created
                but with a different schema.
        """

        if not self._experiment:
            raise ValueError(
                "No experiment set for this run. Make sure to call aiplatform.init(experiment='my-experiment') "
                "before trying to start_run. "
            )

        run_execution_id = f"{self._experiment.name}-{run}"
        run_execution = execution.Execution.get_or_create(
            resource_id=run_execution_id,
            display_name=run,
            schema_title=constants.SYSTEM_RUN,
            schema_version=constants.SCHEMA_VERSIONS[constants.SYSTEM_RUN],
        )
        if run_execution.schema_title != constants.SYSTEM_RUN:
            raise ValueError(
                f"Run name {run} has been used to create other type of resources ({run_execution.schema_title}) "
                "in this MetadataStore, please choose a different run name."
            )

        self._experiment.add_artifacts_and_executions(
            execution_resource_names=[run_execution.resource_name]
        )

        metrics_artifact_id = f"{self._experiment.name}-{run}-metrics"
        metrics_artifact = artifact._Artifact.get_or_create(
            resource_id=metrics_artifact_id,
            display_name=metrics_artifact_id,
            schema_title=constants.SYSTEM_METRICS,
            schema_version=constants.SCHEMA_VERSIONS[constants.SYSTEM_METRICS],
        )
        if metrics_artifact.schema_title != constants.SYSTEM_METRICS:
            raise ValueError(
                f"Run name {run} has been used to create other type of resources ({metrics_artifact.schema_title}) "
                "in this MetadataStore, please choose a different run name."
            )
        run_execution._add_artifact(
            artifact_resource_names=[metrics_artifact.resource_name], input=False
        )

        self._run = run_execution
        self._metrics = metrics_artifact

    def log_params(self, params: Dict[str, Union[float, int, str]]):
        """Log single or multiple parameters with specified key and value pairs.

        Args:
            params (Dict):
                Required. Parameter key/value pairs.
        """

        self._validate_experiment_and_run(method_name="log_params")
        # query the latest run execution resource before logging.
        run_execution = execution.Execution.get_or_create(
            resource_id=self._run.name,
            schema_title=constants.SYSTEM_RUN,
            schema_version=constants.SCHEMA_VERSIONS[constants.SYSTEM_RUN],
        )
        run_execution.update(metadata=params)

    def log_metrics(self, metrics: Dict[str, Union[float, int]]):
        """Log single or multiple Metrics with specified key and value pairs.

        Args:
            metrics (Dict):
                Required. Metrics key/value pairs. Only float and int are supported format for value.
        Raises:
            TypeError: If value contains unsupported types.
            ValueError: If Experiment or Run is not set.
        """

        self._validate_experiment_and_run(method_name="log_metrics")
        self._validate_metrics_value_type(metrics)
        # query the latest metrics artifact resource before logging.
        metric_artifact = artifact.Artifact.get_or_create(
            resource_id=self._metrics.name,
            schema_title=constants.SYSTEM_METRICS,
            schema_version=constants.SCHEMA_VERSIONS[constants.SYSTEM_METRICS],
        )
        metric_artifact.update(metadata=metrics)

    def get_experiment_df(
        self, experiment: Optional[str] = None
    ) -> "pd.DataFrame":  # noqa: F821
        """Returns a Pandas DataFrame of the parameters and metrics associated with one experiment.

        Example:

        aiplatform.init(experiment='exp-1')
        aiplatform.start_run(run='run-1')
        aiplatform.log_params({'learning_rate': 0.1})
        aiplatform.log_metrics({'accuracy': 0.9})

        aiplatform.start_run(run='run-2')
        aiplatform.log_params({'learning_rate': 0.2})
        aiplatform.log_metrics({'accuracy': 0.95})

        Will result in the following DataFrame
        ___________________________________________________________________________
        | experiment_name | run_name      | param.learning_rate | metric.accuracy |
        ---------------------------------------------------------------------------
        | exp-1           | run-1         | 0.1                 | 0.9             |
        | exp-1           | run-2         | 0.2                 | 0.95            |
        ---------------------------------------------------------------------------

        Args:
            experiment (str):
            Name of the Experiment to filter results. If not set, return results of current active experiment.

        Returns:
            Pandas Dataframe of Experiment with metrics and parameters.

        Raise:
            NotFound exception if experiment does not exist.
            ValueError if given experiment is not associated with a wrong schema.
        """

        source = "experiment"
        if not experiment:
            experiment = self._experiment.name
            experiment_resource_name = self._experiment.resource_name
        else:
            experiment_resource_name = self._get_experiment_or_pipeline_resource_name(
                name=experiment,
                source=source,
                expected_schema=constants.SYSTEM_EXPERIMENT,
            )

        return self._query_runs_to_data_frame(
            context_id=experiment,
            context_resource_name=experiment_resource_name,
            source=source,
        )

    def get_pipeline_df(self, pipeline: str) -> "pd.DataFrame":  # noqa: F821
        """Returns a Pandas DataFrame of the parameters and metrics associated with one pipeline.

        Args:
            pipeline: Name of the Pipeline to filter results.

        Returns:
            Pandas Dataframe of Pipeline with metrics and parameters.

        Raise:
            NotFound exception if experiment does not exist.
            ValueError if given experiment is not associated with a wrong schema.
        """

        source = "pipeline"
        pipeline_resource_name = self._get_experiment_or_pipeline_resource_name(
            name=pipeline, source=source, expected_schema=constants.SYSTEM_PIPELINE
        )

        return self._query_runs_to_data_frame(
            context_id=pipeline,
            context_resource_name=pipeline_resource_name,
            source=source,
        )

    def _validate_experiment_and_run(self, method_name: str):
        if not self._experiment:
            raise ValueError(
                f"No experiment set. Make sure to call aiplatform.init(experiment='my-experiment') "
                f"before trying to {method_name}. "
            )
        if not self._run:
            raise ValueError(
                f"No run set. Make sure to call aiplatform.start_run('my-run') before trying to {method_name}. "
            )

    @staticmethod
    def _validate_metrics_value_type(metrics: Dict[str, Union[float, int]]):
        """Verify that metrics value are with supported types.

        Args:
            metrics (Dict):
                Required. Metrics key/value pairs. Only float and int are supported format for value.
        Raises:
            TypeError: If value contains unsupported types.
        """

        for key, value in metrics.items():
            if isinstance(value, int) or isinstance(value, float):
                continue
            raise TypeError(
                f"metrics contain unsupported value types. key: {key}; value: {value}; type: {type(value)}"
            )

    @staticmethod
    def _get_experiment_or_pipeline_resource_name(
        name: str, source: str, expected_schema: str
    ) -> str:
        """Get the full resource name of the Context representing an Experiment or Pipeline.

        Args:
            name (str):
                Name of the Experiment or Pipeline.
            source (str):
                Identify whether the this is an Experiment or a Pipeline.
            expected_schema (str):
                expected_schema identifies the expected schema used for Experiment or Pipeline.

        Returns:
            The full resource name of the Experiment or Pipeline Context.

        Raise:
            NotFound exception if experiment or pipeline does not exist.
        """

        this_context = context._Context(resource_name=name)

        if this_context.schema_title != expected_schema:
            raise ValueError(
                f"Please provide a valid {source} name. {name} is not a {source}."
            )
        return this_context.resource_name

    def _query_runs_to_data_frame(
        self, context_id: str, context_resource_name: str, source: str
    ) -> "pd.DataFrame":  # noqa: F821
        """Get metrics and parameters associated with a given Context into a Dataframe.

        Args:
            context_id (str):
                Name of the Experiment or Pipeline.
            context_resource_name (str):
                Full resource name of the Context associated with an Experiment or Pipeline.
            source (str):
                Identify whether the this is an Experiment or a Pipeline.

        Returns:
            The full resource name of the Experiment or Pipeline Context.
        """

        filter = f'schema_title="{constants.SYSTEM_RUN}" AND in_context("{context_resource_name}")'
        run_executions = execution.Execution.list(filter=filter)

        context_summary = []
        for run_execution in run_executions:
            run_dict = {
                f"{source}_name": context_id,
                "run_name": run_execution.display_name,
            }
            run_dict.update(
                self._execution_to_column_named_metadata(
                    "param", run_execution.metadata
                )
            )

            for metric_artifact in run_execution.query_input_and_output_artifacts():
                run_dict.update(
                    self._execution_to_column_named_metadata(
                        "metric", metric_artifact.metadata
                    )
                )

            context_summary.append(run_dict)

        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "Pandas is not installed and is required to get dataframe as the return format. "
                'Please install the SDK using "pip install python-aiplatform[full]"'
            )

        return pd.DataFrame(context_summary)

    @staticmethod
    def _execution_to_column_named_metadata(
        metadata_type: str, metadata: Dict, filter_prefix: Optional[str] = None
    ) -> Dict[str, Union[int, float, str]]:
        """Returns a dict of the Execution/Artifact metadata with column names.

        Args:
          metadata_type: The type of this execution properties (param, metric).
          metadata: Either an Execution or Artifact metadata field.
          filter_prefix:
            Remove this prefix from the key of metadata field. Mainly used for removing
            "input:" from PipelineJob parameter keys

        Returns:
          Dict of custom properties with keys mapped to column names
        """
        column_key_to_value = {}
        for key, value in metadata.items():
            if filter_prefix and key.startswith(filter_prefix):
                key = key[len(filter_prefix) :]
            column_key_to_value[".".join([metadata_type, key])] = value

        return column_key_to_value


class _ExperimentTracker:
    """Tracks Experiments and Experiment Runs wil high level APIs"""
    def __init__(self):
        self._experiment: Optional[experiment_resources.Experiment] = None
        self._experiment_run: Optional[experiment_run_resource.ExperimentRun] = None

    def reset(self):
        self._experiment = None
        self._experiment_run = None

    @property
    def experiment_name(self) -> Optional[str]:
        """Return the experiment name of the _MetadataService, if experiment is not set, return None"""
        if self._experiment:
            return self._experiment.name
        return None

    @property
    def experiment(self) -> Optional[experiment_resources.Experiment]:
        "Returns the currently set Experiment"
        return self._experiment

    @property
    def experiment_run(self) -> Optional[experiment_run_resource.ExperimentRun]:
        """Returns the currently set experiment run."""
        return self._experiment_run

    def set_experiment(
        self,
        experiment: str,
        *,
        description: Optional[str] = None,
        backing_tensorboard: Optional[
            Union[str, tensorboard_resource.Tensorboard]
        ] = None,
    ):
        """Set the experiment. Will retrieve the Experiment if it exists or create one with the provided name.

        Args:
            experiment (str):
                Required. Name of the experiment to set.
            description (str):
                Optional. Description of an experiment.
            backing_tensorboard Union[str, aiplatform.Tensorboard]:
                Optional. If provided, assigns tensorboard as backing tensorboard to support time series metrics
                logging.
        """
        self.reset()

        experiment = experiment_resources.Experiment.get_or_create(
            experiment_name=experiment, description=description
        )

        if backing_tensorboard:
            experiment.assign_backing_tensorboard(tensorboard=backing_tensorboard)

        self._experiment = experiment

    def start_run(
        self,
        run_name: str,
        *,
        tensorboard: Union[tensorboard_resource.Tensorboard, str, None] = None,
        resume=False,
    ) -> experiment_run_resource.ExperimentRun:
        """Start a run to current session.

        Args:
            run (str):
                Required. Name of the run to assign current session with.
            tensorboard Union[str, tensorboard_resource.Tensorboard]:
                Optional. Backing Tensorboard Resource to enable and store time series metrics
                logged to this Experiment Run using `log_time_series_metrics`.
            resume (bool):
                Whether to resume this run. If False a new run will be created.
        Raises:
            ValueError:
                if experiment is not set. Or if run execution or metrics artifact is already created
                but with a different schema.
        """

        if not self._experiment:
            raise ValueError(
                "No experiment set for this run. Make sure to call aiplatform.init(experiment='my-experiment') "
                "before invoking start_run. "
            )

        if self._experiment_run:
            self.end_run()

        if resume:
            self._experiment_run = experiment_run_resource.ExperimentRun(
                run_name=run_name, experiment=self._experiment
            )
            if tensorboard:
                self._experiment_run.assign_backing_tensorboard(tensorboard=tensorboard)

            self._experiment_run.update_state(state=gapic.Execution.State.RUNNING)

        else:
            self._experiment_run = experiment_run_resource.ExperimentRun.create(
                run_name=run_name, experiment=self._experiment, tensorboard=tensorboard
            )

        return self._experiment_run

    def end_run(self, state: gapic.Execution.State = gapic.Execution.State.COMPLETE):
        "Ends the the current Experiment Run."
        self._validate_experiment_and_run(method_name='end_run')
        try:
            self._experiment_run.end_run(state=state)
        except exceptions.NotFound:
            _LOGGER.warn(f'Experiment run {self._experiment_run.name} was not found.'
                         'It may have been deleted')
        finally:
            self._experiment_run = None

    def log_params(self, params: Dict[str, Union[float, int, str]]):
        """Log single or multiple parameters with specified key and value pairs.

        Args:
            params (Dict):
                Required. Parameter key/value pairs.
        """

        self._validate_experiment_and_run(method_name="log_params")
        # query the latest run execution resource before logging.
        self._experiment_run.log_params(params=params)

    def log_metrics(self, metrics: Dict[str, Union[float, int]]):
        """Log single or multiple Metrics with specified key and value pairs.

        Args:
            metrics (Dict):
                Required. Metrics key/value pairs. Only flot and int are supported format for value.
        Raises:
            ValueError: If Experiment or Run is not set.
        """

        self._validate_experiment_and_run(method_name="log_metrics")
        # query the latest metrics artifact resource before logging.
        self._experiment_run.log_metrics(metrics=metrics)

    def _validate_experiment_and_run(self, method_name: str):
        """Validates Experiment and Run are set and raises informative error message.

        Raises:
            ValueError: If Experiment or Run are not set.
        """

        if not self._experiment:
            raise ValueError(
                f"No experiment set. Make sure to call aiplatform.init(experiment='my-experiment') "
                f"before trying to {method_name}. "
            )
        if not self._experiment_run:
            raise ValueError(
                f"No run set. Make sure to call aiplatform.start_run('my-run') before trying to {method_name}. "
            )

    def get_experiment_df(
        self, experiment: Optional[str] = None
    ) -> "pd.DataFrame":  # noqa: F821
        """Returns a Pandas DataFrame of the parameters and metrics associated with one experiment.

        Example:

        aiplatform.init(experiment='exp-1')
        aiplatform.start_run(run='run-1')
        aiplatform.log_params({'learning_rate': 0.1})
        aiplatform.log_metrics({'accuracy': 0.9})

        aiplatform.start_run(run='run-2')
        aiplatform.log_params({'learning_rate': 0.2})
        aiplatform.log_metrics({'accuracy': 0.95})

        Will result in the following DataFrame
        ___________________________________________________________________________
        | experiment_name | run_name      | param.learning_rate | metric.accuracy |
        ---------------------------------------------------------------------------
        | exp-1           | run-1         | 0.1                 | 0.9             |
        | exp-1           | run-2         | 0.2                 | 0.95            |
        ---------------------------------------------------------------------------

        Args:
            experiment (str):
            Name of the Experiment to filter results. If not set, return results of current active experiment.

        Returns:
            Pandas Dataframe of Experiment with metrics and parameters.

        Raise:
            NotFound exception if experiment does not exist.
            ValueError if given experiment is not associated with a wrong schema.
        """

        if not experiment:
            experiment = self._experiment
        else:
            experiment = experiment_resources.Experiment(experiment)

        return experiment.get_data_frame()

    def log(
        self,
        *,
        pipeline_job: Optional[pipeline_jobs.PipelineJob] = None,
    ):
        """Log Vertex AI Resources and Artifacts to the current Experiment Run.

        Args:
            pipeline_job (pipeline_jobs.PipelineJob):
                Optional. Vertex PipelineJob to associate to this Experiment Run.

                Metrics produced by the PipelineJob as system.Metric Artifacts
                will be associated as metrics to the current Experiment Run.

                Pipeline parameters will be associated as parameters to the
                current Experiment Run.
        """
        self._validate_experiment_and_run(method_name="log")
        self._experiment_run.log(pipeline_job=pipeline_job)

    def log_time_series_metrics(
        self,
        metrics: Dict[str, Union[float]],
        step: Optional[int] = None,
        wall_time: Optional[timestamp_pb2.Timestamp] = None,
    ):
        """Logs time series metrics to to this Experiment Run.

        Requires the Experiment Run has a backing Vertex Tensorboard resource.


        Usage:
            run.log_time_series_metrics({'accuracy': 0.9}, step=10)

        Args:
            metrics (Dict[str, Union[str, float]]):
                Required. Dictionary of where keys are metric names and values are metric values.
            step (int):
                Optional. Step index of this data point within the run.

                If not provided, the latest
                step amongst all time series metrics already logged will be used.
            wall_time (timestamp_pb2.Timestamp):
                Optional. Wall clock timestamp when this data point is
                generated by the end user.

                If not provided, this will be generated based on the value from time.time()

        Raises:
            RuntimeError: If current experiment run doesn't have a backing Tensorboard resource.
        """
        self._validate_experiment_and_run(method_name='log_time_series_metrics')
        self._experiment_run.log_time_series_metrics(
            metrics=metrics, step=step, wall_time=wall_time
        )

    def get_artifact(
        self,
        *,
        uri: Optional[str] = None,
        artifact_name: Optional[str] = None,
        assign_as_input=False,
    ):
        self._validate_experiment_and_run(method_name="get_artifact")

        if bool(uri) == bool(artifact_name):
            raise ValueError(
                "To get an artifact, provide only one of `uri` or `artifact_name`"
            )

        if artifact_name:
            # TODO(compose artifact name if only provided resource_id)
            artifact = artifact.Artifact(resource_name=artifact_name)
        else:
            artifact = artifact.Artifact.get_with_uri(uri=uri)

        if assign_as_input:
            self._experiment_run.assign_artifact_as_input(artifact=artifact)

        return artifact

    # TODO: move inside of experiment run
    def start_execution(
            self,
            *,
            schema_title: Optional[str]=None,
            display_name: Optional[str]=None,
            resource_id: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            schema_version: Optional[str] = None,
            description: Optional[str] = None,
            resume: bool=False,
            project: Optional[str] = None,
            location: Optional[str] = None,
            credentials: Optional[auth_credentials.Credentials] = None) -> execution.Execution:
        """
            Create and starts a new Metadata Execution or resumes a previously created Execution.

            To start a new execution:

            ```
            with aiplatform.start_execution(schema_title='system.ContainerExecution', display_name='trainer) as exc:
              exc.assign_input_artifacts([my_artifact])
              model = aiplatform.Artifact.create(uri='gs://my-uri', schema_title='system.Model')
              exc.assign_output_artifacts([model])
            ```

            To continue a previously created execution:
            ```
            with aiplatform.start_execution(resource_id='my-exc', resume=True) as exc:
                ...
            ```
            Args:
                schema_title (str):
                    Optional. schema_title identifies the schema title used by the Execution. Required if starting
                    a new Execution.
                state (gca_execution.Execution.State.RUNNING):
                    Optional. State of this Execution. Defaults to RUNNING.
                resource_id (str):
                    Optional. The <resource_id> portion of the Execution name with
                    the format. This is globally unique in a metadataStore:
                    projects/123/locations/us-central1/metadataStores/<metadata_store_id>/executions/<resource_id>.
                display_name (str):
                    Optional. The user-defined name of the Execution.
                schema_version (str):
                    Optional. schema_version specifies the version used by the Execution.
                    If not set, defaults to use the latest version.
                metadata (Dict):
                    Optional. Contains the metadata information that will be stored in the Execution.
                description (str):
                    Optional. Describes the purpose of the Execution to be created.
                metadata_store_id (str):
                    Optional. The <metadata_store_id> portion of the resource name with
                    the format:
                    projects/123/locations/us-central1/metadataStores/<metadata_store_id>/artifacts/<resource_id>
                    If not provided, the MetadataStore's ID will be set to "default".
                project (str):
                    Optional. Project used to create this Execution. Overrides project set in
                    aiplatform.init.
                location (str):
                    Optional. Location used to create this Execution. Overrides location set in
                    aiplatform.init.
                credentials (auth_credentials.Credentials):
                    Optional. Custom credentials used to create this Execution. Overrides
                    credentials set in aiplatform.init.

            Returns:
                Execution: Instantiated representation of the managed Metadata Execution.

            Raises:
                ValueError: If experiment run is set and project or location do not match experiment run.
                ValueError: If resume set to `True` and resource_id is not provided.
                ValueError: If creating a new executin and schema_title is not provided.
        """

        if self._experiment_run and not self._experiment_run._is_legacy_experiment_run():
            if project and project != self._experiment_run.project:
                raise ValueError(f'Currently set Experiment run project {self._experiment_run.project} must'
                                 f'match provided project {project}')
            if location and location != self._experiment_run.location:
                raise ValueError(f'Currently set Experiment run location {self._experiment_run.location} must'
                                 f'match provided location {project}')

        if resume:
            if not resource_id:
                raise ValueError('resource_id is required when resume=True')

            run_execution = execution.Execution(execution_name=resource_id,
                                                project=project,
                                                location=location,
                                                credentials=credentials)

            # TODO(handle updates if resuming)

            run_execution.update(state=gca_execution.Execution.State.RUNNING)
        else:
            if not schema_title:
                raise ValueError('schema_title must be provided when starting a new Execution')

            run_execution = execution.Execution.create(
                display_name=display_name,
                schema_title=schema_title,
                schema_version=schema_version,
                metadata=metadata,
                description=description,
                resource_id=resource_id,
                project = project,
                location = location,
                credentials= credentials
            )

        if self.experiment_run:
            if self.experiment_run._is_legacy_experiment_run():
                _LOGGER.warn(
                    f'{self.experiment_run._run_name} is an Experiment run created in Vertex Experiment Preview',
                    f' and does not support tracking Executions.'
                    f' Please create a new Experiment run to track executions against an Experiment run.')
            else:
                self.experiment_run.associate_execution(run_execution)
                run_execution.assign_input_artifacts = self.experiment_run._association_wrapper(
                    run_execution.assign_input_artifacts
                )
                run_execution.assign_output_artifacts = self.experiment_run._association_wrapper(
                    run_execution.assign_output_artifacts
                )

        return run_execution



metadata_service = _MetadataService()
experiment_tracker = _ExperimentTracker()
