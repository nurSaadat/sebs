import docker
import os
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from typing import cast, Dict, Optional, Tuple, List, Type

from googleapiclient.discovery import build
from google.cloud import monitoring_v3

from sebs.cache import Cache
from sebs.config import SeBSConfig
from sebs.benchmark import Benchmark
from sebs import utils
from ..faas.function import Function, Trigger
from .storage import PersistentStorage
from ..faas.system import System
from sebs.gcp.config import GCPConfig
from sebs.gcp.storage import GCPStorage
from sebs.gcp.function import GCPFunction
from sebs.utils import LoggingHandlers

"""
    This class provides basic abstractions for the FaaS system.
    It provides the interface for initialization of the system and storage
    services, creation and update of serverless functions and querying
    logging and measurements services to obtain error messages and performance
    measurements.
"""


class GCP(System):
    def __init__(
        self,
        system_config: SeBSConfig,
        config: GCPConfig,
        cache_client: Cache,
        docker_client: docker.client,
        logging_handlers: LoggingHandlers,
    ):
        super().__init__(system_config, cache_client, docker_client)
        self._config = config
        self.storage: Optional[GCPStorage] = None
        self.logging_handlers = logging_handlers

    @property
    def config(self) -> GCPConfig:
        return self._config

    @staticmethod
    def name():
        return "gcp"

    @staticmethod
    def typename():
        return "GCP"

    @staticmethod
    def function_type() -> "Type[Function]":
        return GCPFunction

    """
        Initialize the system. After the call the local or remote
        FaaS system should be ready to allocate functions, manage
        storage resources and invoke functions.

        :param config: systems-specific parameters
    """

    def initialize(self, config: Dict[str, str] = {}):
        self.function_client = build("cloudfunctions", "v1", cache_discovery=False)
        self.get_storage()

    def get_function_client(self):
        return self.function_client

    """
        Access persistent storage instance.
        It might be a remote and truly persistent service (AWS S3, Azure Blob..),
        or a dynamically allocated local instance.

        :param replace_existing: replace benchmark input data if exists already
    """

    def get_storage(
        self, replace_existing: bool = False, benchmark=None, buckets=None,
    ) -> PersistentStorage:
        if not self.storage:
            self.storage = GCPStorage(self.cache_client, replace_existing)
            self.storage.logging_handlers = self.logging_handlers
        else:
            self.storage.replace_existing = replace_existing
        return self.storage

    @staticmethod
    def default_function_name(code_package: Benchmark) -> str:
        # Create function name
        func_name = "{}-{}-{}".format(
            code_package.benchmark,
            code_package.language_name,
            code_package.benchmark_config.memory,
        )
        return GCP.format_function_name(func_name)

    @staticmethod
    def format_function_name(func_name: str) -> str:
        # GCP functions must begin with a letter
        func_name = func_name.replace("-", "_")
        func_name = func_name.replace(".", "_")
        return f"function-{func_name}"

    """
        Apply the system-specific code packaging routine to build benchmark.
        The benchmark creates a code directory with the following structure:
        - [benchmark sources]
        - [benchmark resources]
        - [dependence specification], e.g. requirements.txt or package.json
        - [handlers implementation for the language and deployment]

        This step allows us to change the structure above to fit different
        deployment requirements, Example: a zip file for AWS or a specific
        directory structure for Azure.

        :return: path to packaged code and its size
    """

    def package_code(
        self, directory: str, language_name: str, benchmark: str
    ) -> Tuple[str, int]:

        CONFIG_FILES = {
            "python": ["handler.py", ".python_packages"],
            "nodejs": ["handler.js", "node_modules"],
        }
        HANDLER = {
            "python": ("handler.py", "main.py"),
            "nodejs": ("handler.js", "index.js"),
        }
        package_config = CONFIG_FILES[language_name]
        function_dir = os.path.join(directory, "function")
        os.makedirs(function_dir)
        for file in os.listdir(directory):
            if file not in package_config:
                file = os.path.join(directory, file)
                shutil.move(file, function_dir)

        requirements = open(os.path.join(directory, "requirements.txt"), "w")
        requirements.write("google-cloud-storage")
        requirements.close()

        cur_dir = os.getcwd()
        os.chdir(directory)
        old_name, new_name = HANDLER[language_name]
        shutil.move(old_name, new_name)

        utils.execute("zip -qu -r9 {}.zip * .".format(benchmark), shell=True)
        benchmark_archive = "{}.zip".format(os.path.join(directory, benchmark))
        logging.info("Created {} archive".format(benchmark_archive))

        bytes_size = os.path.getsize(benchmark_archive)
        mbytes = bytes_size / 1024.0 / 1024.0
        logging.info("Zip archive size {:2f} MB".format(mbytes))
        shutil.move(new_name, old_name)
        os.chdir(cur_dir)
        return os.path.join(directory, "{}.zip".format(benchmark)), bytes_size

    def create_function(self, code_package: Benchmark, func_name: str) -> "GCPFunction":

        package = code_package.code_location
        benchmark = code_package.benchmark
        language_runtime = code_package.language_version
        timeout = code_package.benchmark_config.timeout
        memory = code_package.benchmark_config.memory
        code_bucket: Optional[str] = None
        storage_client = self.get_storage()
        location = self.config.region
        project_name = self.config.project_name

        code_package_name = cast(str, os.path.basename(package))
        code_bucket, idx = storage_client.add_input_bucket(benchmark)
        storage_client.upload(code_bucket, package, code_package_name)
        self.logging.info(
            "Uploading function {} code to {}".format(func_name, code_bucket)
        )

        req = (
            self.function_client.projects()
            .locations()
            .functions()
            .list(
                parent="projects/{project_name}/locations/{location}".format(
                    project_name=project_name, location=location
                )
            )
        )
        res = req.execute()

        full_func_name = GCP.get_full_function_name(project_name, location, func_name)
        if "functions" in res.keys() and full_func_name in [
            f["name"] for f in res["functions"]
        ]:
            # FIXME: retrieve existing configuration, update code and return object
            raise NotImplementedError()
            # self.update_function(
            #    benchmark,
            #    full_func_name,
            #    code_package_name,
            #    code_package,
            #    timeout,
            #    memory,
            # )
        else:
            language_runtime = code_package.language_version
            print(
                "language runtime: ",
                code_package.language_name + language_runtime.replace(".", ""),
            )
            req = (
                self.function_client.projects()
                .locations()
                .functions()
                .create(
                    location="projects/{project_name}/locations/{location}".format(
                        project_name=project_name, location=location
                    ),
                    body={
                        "name": full_func_name,
                        "entryPoint": "handler",
                        "runtime": code_package.language_name
                        + language_runtime.replace(".", ""),
                        "availableMemoryMb": memory,
                        "timeout": str(timeout) + "s",
                        "httpsTrigger": {},
                        "ingressSettings": "ALLOW_ALL",
                        "sourceArchiveUrl": "gs://"
                        + code_bucket
                        + "/"
                        + code_package_name,
                    },
                )
            )
            print("request: ", req)
            res = req.execute()
            print("response:", res)
            self.logging.info(f"Function {func_name} has been created!")
            req = (
                self.function_client.projects()
                .locations()
                .functions()
                .setIamPolicy(
                    resource=full_func_name,
                    body={
                        "policy": {
                            "bindings": [
                                {
                                    "role": "roles/cloudfunctions.invoker",
                                    "members": "allUsers",
                                }
                            ]
                        }
                    },
                )
            )
            res = req.execute()
            print(res)
            self.logging.info(
                f"Function {func_name} accepts now unauthenticated invocations!"
            )

            function = GCPFunction(
                func_name, benchmark, code_package.hash, timeout, memory, code_bucket
            )

        # Add LibraryTrigger to a new function
        from sebs.gcp.triggers import LibraryTrigger, HTTPTrigger

        trigger = LibraryTrigger(func_name, self)
        trigger.logging_handlers = self.logging_handlers
        function.add_trigger(trigger)

        return function

    def create_trigger(
        self, function: Function, trigger_type: Trigger.TriggerType
    ) -> Trigger:
        from sebs.gcp.triggers import HTTPTrigger

        if trigger_type == Trigger.TriggerType.HTTP:

            location = self.config.region
            project_name = self.config.project_name
            full_func_name = GCP.get_full_function_name(project_name, location, function.name)
            self.logging.info(f"Function {function.name} - waiting for deployment...")
            our_function_req = (
                self.function_client.projects()
                .locations()
                .functions()
                .get(name=full_func_name)
            )
            deployed = False
            while not deployed:
                status_res = our_function_req.execute()
                if status_res["status"] == "ACTIVE":
                    deployed = True
                else:
                    time.sleep(3)
            self.logging.info(f"Function {func_name} - deployed!")
            invoke_url = status_res["httpsTrigger"]["url"]

            http_trigger = HTTPTrigger(invoke_url)
            http_trigger.logging_handlers = self.logging_handlers
            function.add_trigger(http_trigger)
        else:
            raise RuntimeError("Not supported!")

        function.add_trigger(trigger)
        self.cache_client.update_function(function)
        return trigger

    def cached_function(self, function: Function):

        from sebs.faas.function import Trigger
        from sebs.gcp.triggers import LibraryTrigger

        for trigger in function.triggers(Trigger.TriggerType.LIBRARY):
            gcp_trigger = cast(LibraryTrigger, trigger)
            gcp_trigger.logging_handlers = self.logging_handlers
            gcp_trigger.deployment_client = self

    def update_function(self, function: Function, code_package: Benchmark):

        function = cast(GCPFunction, function)
        language_runtime = code_package.language_version
        code_package_name = os.path.basename(code_package.code_location)
        storage = cast(GCPStorage, self.get_storage())
        bucket = function.code_bucket(code_package.benchmark, storage)
        full_func_name = GCP.get_full_function_name(
            self.config.project_name, self.config.region, function.name
        )
        req = (
            self.function_client.projects()
            .locations()
            .functions()
            .patch(
                name=full_func_name,
                body={
                    "name": full_func_name,
                    "entryPoint": "handler",
                    "runtime": code_package.language_name
                    + language_runtime.replace(".", ""),
                    "availableMemoryMb": function.memory,
                    "timeout": str(function.timeout) + "s",
                    "httpsTrigger": {},
                    "sourceArchiveUrl": "gs://" + bucket + "/" + code_package_name,
                },
            )
        )
        res = req.execute()
        print("response:", res)
        self.logging.info("Published new function code")

    @staticmethod
    def get_full_function_name(project_name: str, location: str, func_name: str):
        return f"projects/{project_name}/locations/{location}/functions/{func_name}"

    def prepare_experiment(self, benchmark):
        logs_bucket = self.storage.add_output_bucket(benchmark, suffix="logs")
        return logs_bucket

    def shutdown(self) -> None:
        try:
            self.cache_client.lock()
            self.config.update_cache(self.cache_client)
        finally:
            self.cache_client.unlock()

    def download_metrics(
        self, function_name: str, start_time: int, end_time: int, requests: dict,
    ):

        """
            Use GCP's logging system to find execution time of each function invocation.

            There shouldn't be problem of waiting for complete results,
            since logs appear very quickly here.
        """
        from google.cloud import logging as gcp_logging

        logging_client = gcp_logging.Client()
        logger = logging_client.logger(
            "cloudfunctions.googleapis.com%2Fcloud-functions"
        )

        """
            GCP accepts only single date format: 'YYYY-MM-DDTHH:MM:SSZ'.
            Thus, we first convert timestamp to UTC timezone.
            Then, we generate correct format.

            Add 1 second to end time to ensure that removing
            milliseconds doesn't affect query.
        """
        timestamps = []
        for timestamp in [start_time, end_time + 1]:
            utc_date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            timestamps.append(utc_date.strftime("%Y-%m-%dT%H:%M:%SZ"))

        invocations = logger.list_entries(
            filter_=(f'resource.labels.function_name = "{function_name}" '
                    f'timestamp >= "{timestamps[0]}" '
                    f'timestamp <= "{timestamps[1]}"')
        )
        invocations_processed = 0
        for invoc in invocations:
            if "execution took" in invoc.payload:
                execution_id = invoc.labels["execution_id"]
                # might happen that we get invocation from another experiment
                if execution_id not in requests:
                    continue
                # find number of miliseconds
                exec_time = re.search(r"\d+ ms", invoc.payload).group().split()[0]
                requests[execution_id].provider_times.execution = int(exec_time)
                invocations_processed += 1
        self.logging.info(
            f"GCP: Found time metrics for {invocations_processed} "
            f"out of {len(requests.keys())} invocations."
        )

        """
            Use metrics to find estimated values for maximum memory used, active instances
            and network traffic.
            https://cloud.google.com/monitoring/api/metrics_gcp#gcp-cloudfunctions
        """
        # FIXME: temporary disable
        # client = monitoring_v3.MetricServiceClient()
        # project_name = client.common_project_path(self.config.project_name)
        # interval = monitoring_v3.types.TimeInterval()
        # print(interval.start_time)
        # interval.start_time.seconds = int(start_time - 60)
        # interval.end_time.seconds = int(end_time + 60)

        # results = client.list_time_series(
        #    project_name,
        #    'metric.type = "cloudfunctions.googleapis.com/function/execution_times"',
        #    interval,
        #    monitoring_v3.enums.ListTimeSeriesRequest.TimeSeriesView.FULL,
        # )
        # for result in results:
        #    if result.resource.labels.get("function_name") == function_name:
        #        for point in result.points:
        #            requests[function_name]["execution_times"] += [
        #                {
        #                    "mean_time": point.value.distribution_value.mean,
        #                    "executions_count": point.value.distribution_value.count,
        #                }
        #            ]

        # results = client.list_time_series(
        #    project_name,
        #    'metric.type = "cloudfunctions.googleapis.com/function/user_memory_bytes"',
        #    interval,
        #    monitoring_v3.enums.ListTimeSeriesRequest.TimeSeriesView.FULL,
        # )
        # for result in results:
        #    if result.resource.labels.get("function_name") == function_name:
        #        for point in result.points:
        #            requests[function_name]["user_memory_bytes"] += [
        #                {
        #                    "mean_memory": point.value.distribution_value.mean,
        #                    "executions_count": point.value.distribution_value.count,
        #                }
        #            ]

    def enforce_cold_start(self, function: Function, code_package: Benchmark):
        pass

    def enforce_cold_starts(self, function: List[Function], code_package: Benchmark):
        pass

    def get_functions(self, code_package: Benchmark, function_names: List[str]) -> List["Function"]:

        functions: List["Function"] = []
        undeployed_functions_before = []
        for func_name in function_names:
            func = self.get_function(code_package, func_name)
            functions.append(func)
            undeployed_functions_before.append(func)

        # verify deployment
        undeployed_functions = []
        deployment_done = False
        while not deployment_done:
            for func in functions:
                if not self.is_deployed(func.name):
                    undeployed_functions.append(func)
            deployed = len(undeployed_functions_before) - len(undeployed_functions)
            self.logging.info(f"Deployed {deployed} out of {len(undeployed_functions_before)}")
            if deployed == len(undeployed_functions_before):
                deployment_done = True
                break
            time.sleep(5)
            undeployed_functions_before = undeployed_functions
            undeployed_functions = []

        return functions

    def is_deployed(self, func_name: str) -> bool:
        name = GCP.get_full_function_name(self.config.project_name, self.config.region, func_name)
        function_client = self.get_function_client()
        status_req = (
            function_client.projects().locations().functions().get(name=name)
        )
        status_res = status_req.execute()
        return status_res["status"] == "ACTIVE"

    def deployment_version(self, func: Function) -> int:
        name = GCP.get_full_function_name(self.config.project_name, self.config.region, func_name)
        function_client = self.deployment_client.get_function_client()
        status_req = (
            function_client.projects().locations().functions().get(name=name)
        )
        status_res = status_req.execute()
        return int(status_res["versionId"])

    # @abstractmethod
    # def get_invocation_error(self, function_name: str,
    #   start_time: int, end_time: int):
    #    pass

    # @abstractmethod
    # def download_metrics(self):
    #    pass
