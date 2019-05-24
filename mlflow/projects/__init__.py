"""
The ``mlflow.projects`` module provides an API for running MLflow projects locally or remotely.
"""

from __future__ import print_function

from distutils import dir_util
import hashlib
import json
import os
import sys
import re
import shutil
import subprocess
import tempfile
import logging
import posixpath
import docker

import mlflow.tracking as tracking
import mlflow.tracking.fluent as fluent
import mlflow.tracking.context as context
from mlflow.projects.submitted_run import LocalSubmittedRun, SubmittedRun
from mlflow.projects import _project_spec
from mlflow.exceptions import ExecutionException, MlflowException
from mlflow.entities import RunStatus, SourceType
from mlflow.tracking.fluent import _get_experiment_id
from mlflow.tracking.context import _get_git_commit
import mlflow.projects.databricks
from mlflow.utils import process
from mlflow.utils.mlflow_tags import MLFLOW_PROJECT_ENV, MLFLOW_DOCKER_IMAGE_NAME, \
    MLFLOW_DOCKER_IMAGE_ID, MLFLOW_USER, MLFLOW_SOURCE_NAME, MLFLOW_SOURCE_TYPE, \
    MLFLOW_GIT_COMMIT, MLFLOW_GIT_REPO_URL, MLFLOW_GIT_BRANCH, LEGACY_MLFLOW_GIT_REPO_URL, \
    LEGACY_MLFLOW_GIT_BRANCH_NAME, MLFLOW_PROJECT_ENTRY_POINT, MLFLOW_PARENT_RUN_ID
from mlflow.utils import databricks_utils, file_utils

# TODO: this should be restricted to just Git repos and not S3 and stuff like that
_GIT_URI_REGEX = re.compile(r"^[^/]*:")
_FILE_URI_REGEX = re.compile(r"^file://.+")
_ZIP_URI_REGEX = re.compile(r".+\.zip$")
# Environment variable indicating a path to a conda installation. MLflow will default to running
# "conda" if unset
MLFLOW_CONDA_HOME = "MLFLOW_CONDA_HOME"
_GENERATED_DOCKERFILE_NAME = "Dockerfile.mlflow-autogenerated"
_PROJECT_TAR_ARCHIVE_NAME = "mlflow-project-docker-build-context"
_MLFLOW_DOCKER_TRACKING_DIR_PATH = "/mlflow/tmp/mlruns"

_logger = logging.getLogger(__name__)


def _resolve_experiment_id(experiment_name=None, experiment_id=None):
    """
    Resolve experiment.

    Verifies either one or other is specified - cannot be both selected.

    :param experiment_name: Name of experiment under which to launch the run.
    :param experiment_id: ID of experiment under which to launch the run.
    :return: int
    """

    if experiment_name and experiment_id:
        raise MlflowException("Specify only one of 'experiment_name' or 'experiment_id'.")

    exp_id = experiment_id
    if experiment_name:
        client = tracking.MlflowClient()
        exp_id = client.get_experiment_by_name(experiment_name).experiment_id
    exp_id = exp_id or _get_experiment_id()
    return exp_id


def _run(uri, experiment_id, entry_point="main", version=None, parameters=None,
         backend=None, backend_config=None, use_conda=True,
         storage_dir=None, synchronous=True, run_id=None):
    """
    Helper that delegates to the project-running method corresponding to the passed-in backend.
    Returns a ``SubmittedRun`` corresponding to the project run.
    """

    parameters = parameters or {}
    work_dir = _fetch_project(uri=uri, force_tempdir=False, version=version)
    project = _project_spec.load_project(work_dir)
    _validate_execution_environment(project, backend)
    project.get_entry_point(entry_point)._validate_parameters(parameters)
    if run_id:
        active_run = tracking.MlflowClient().get_run(run_id)
    else:
        active_run = _create_run(uri, experiment_id, work_dir, entry_point)

    # Consolidate parameters for logging.
    # `storage_dir` is `None` since we want to log actual path not downloaded local path
    entry_point_obj = project.get_entry_point(entry_point)
    final_params, extra_params = entry_point_obj.compute_parameters(parameters, storage_dir=None)
    for key, value in (list(final_params.items()) + list(extra_params.items())):
        tracking.MlflowClient().log_param(active_run.info.run_id, key, value)

    repo_url = _get_git_repo_url(work_dir)
    if repo_url is not None:
        for tag in [MLFLOW_GIT_REPO_URL, LEGACY_MLFLOW_GIT_REPO_URL]:
            tracking.MlflowClient().set_tag(active_run.info.run_id, tag, repo_url)

    # Add branch name tag if a branch is specified through -version
    if _is_valid_branch_name(work_dir, version):
        for tag in [MLFLOW_GIT_BRANCH, LEGACY_MLFLOW_GIT_BRANCH_NAME]:
            tracking.MlflowClient().set_tag(active_run.info.run_id, tag, version)

    if backend == "databricks":
        from mlflow.projects.databricks import run_databricks
        return run_databricks(
            remote_run=active_run,
            uri=uri, entry_point=entry_point, work_dir=work_dir, parameters=parameters,
            experiment_id=experiment_id, cluster_spec=backend_config)

    elif backend == "local" or backend is None:
        command = []
        command_separator = " "
        # If a docker_env attribute is defined in MLproject then it takes precedence over conda yaml
        # environments, so the project will be executed inside a docker container.
        if project.docker_env:
            tracking.MlflowClient().set_tag(active_run.info.run_id, MLFLOW_PROJECT_ENV, "docker")
            _validate_docker_env(project.docker_env)
            _validate_docker_installation()
            image = _build_docker_image(work_dir=work_dir,
                                        project=project,
                                        active_run=active_run)
            command += _get_docker_command(image=image, active_run=active_run)
        # Synchronously create a conda environment (even though this may take some time)
        # to avoid failures due to multiple concurrent attempts to create the same conda env.
        elif use_conda:
            tracking.MlflowClient().set_tag(active_run.info.run_id, MLFLOW_PROJECT_ENV, "conda")
            command_separator = " && "
            conda_env_name = _get_or_create_conda_env(project.conda_env_path)
            command += _get_conda_command(conda_env_name)
        # In synchronous mode, run the entry point command in a blocking fashion, sending status
        # updates to the tracking server when finished. Note that the run state may not be
        # persisted to the tracking server if interrupted
        if synchronous:
            command += _get_entry_point_command(project, entry_point, parameters, storage_dir)
            command = command_separator.join(command)
            return _run_entry_point(command, work_dir, experiment_id,
                                    run_id=active_run.info.run_id)
        # Otherwise, invoke `mlflow run` in a subprocess
        return _invoke_mlflow_run_subprocess(
            work_dir=work_dir, entry_point=entry_point, parameters=parameters,
            experiment_id=experiment_id,
            use_conda=use_conda, storage_dir=storage_dir, run_id=active_run.info.run_id)
    supported_backends = ["local", "databricks"]
    raise ExecutionException("Got unsupported execution mode %s. Supported "
                             "values: %s" % (backend, supported_backends))


def run(uri, entry_point="main", version=None, parameters=None,
        experiment_name=None, experiment_id=None,
        backend=None, backend_config=None, use_conda=True,
        storage_dir=None, synchronous=True, run_id=None):
    """
    Run an MLflow project. The project can be local or stored at a Git URI.

    You can run the project locally or remotely on a Databricks.

    For information on using this method in chained workflows, see `Building Multistep Workflows
    <../projects.html#building-multistep-workflows>`_.

    :raises ``ExecutionException``: If a run launched in blocking mode is unsuccessful.

    :param uri: URI of project to run. A local filesystem path
                or a Git repository URI (e.g. https://github.com/mlflow/mlflow-example)
                pointing to a project directory containing an MLproject file.
    :param entry_point: Entry point to run within the project. If no entry point with the specified
                        name is found, runs the project file ``entry_point`` as a script,
                        using "python" to run ``.py`` files and the default shell (specified by
                        environment variable ``$SHELL``) to run ``.sh`` files.
    :param version: For Git-based projects, either a commit hash or a branch name.
    :param experiment_name: Name of experiment under which to launch the run.
    :param experiment_id: ID of experiment under which to launch the run.
    :param backend: Execution backend for the run: "local" or "databricks". If running against
                    Databricks, will run against a Databricks workspace determined as follows: if
                    a Databricks tracking URI of the form ``databricks://profile`` has been set
                    (e.g. by setting the MLFLOW_TRACKING_URI environment variable), will run
                    against the workspace specified by <profile>. Otherwise, runs against the
                    workspace specified by the default Databricks CLI profile.
    :param backend_config: A dictionary, or a path to a JSON file (must end in '.json'), which will
                           be passed as config to the backend. For the Databricks backend, this
                           should be a cluster spec: see `Databricks Cluster Specs for Jobs
                           <https://docs.databricks.com/api/latest/jobs.html#jobsclusterspecnewcluster>`_
                           for more information.
    :param use_conda: If True (the default), create a new Conda environment for the run and
                      install project dependencies within that environment. Otherwise, run the
                      project in the current environment without installing any project
                      dependencies.
    :param storage_dir: Used only if ``backend`` is "local". MLflow downloads artifacts from
                        distributed URIs passed to parameters of type ``path`` to subdirectories of
                        ``storage_dir``.
    :param synchronous: Whether to block while waiting for a run to complete. Defaults to True.
                        Note that if ``synchronous`` is False and ``backend`` is "local", this
                        method will return, but the current process will block when exiting until
                        the local run completes. If the current process is interrupted, any
                        asynchronous runs launched via this method will be terminated.
    :param run_id: Note: this argument is used internally by the MLflow project APIs and should
                   not be specified. If specified, the run ID will be used instead of
                   creating a new run.
    :return: :py:class:`mlflow.projects.SubmittedRun` exposing information (e.g. run ID)
             about the launched run.
    """

    cluster_spec_dict = backend_config
    if (backend_config and type(backend_config) != dict
            and os.path.splitext(backend_config)[-1] == ".json"):
        with open(backend_config, 'r') as handle:
            try:
                cluster_spec_dict = json.load(handle)
            except ValueError:
                _logger.error(
                    "Error when attempting to load and parse JSON cluster spec from file %s",
                    backend_config)
                raise

    if backend == "databricks":
        mlflow.projects.databricks.before_run_validations(mlflow.get_tracking_uri(), backend_config)

    experiment_id = _resolve_experiment_id(experiment_name=experiment_name,
                                           experiment_id=experiment_id)

    submitted_run_obj = _run(
        uri=uri, experiment_id=experiment_id, entry_point=entry_point, version=version,
        parameters=parameters, backend=backend, backend_config=cluster_spec_dict,
        use_conda=use_conda, storage_dir=storage_dir, synchronous=synchronous, run_id=run_id)
    if synchronous:
        _wait_for(submitted_run_obj)
    return submitted_run_obj


def _wait_for(submitted_run_obj):
    """Wait on the passed-in submitted run, reporting its status to the tracking server."""
    run_id = submitted_run_obj.run_id
    active_run = None
    # Note: there's a small chance we fail to report the run's status to the tracking server if
    # we're interrupted before we reach the try block below
    try:
        active_run = tracking.MlflowClient().get_run(run_id) if run_id is not None else None
        if submitted_run_obj.wait():
            _logger.info("=== Run (ID '%s') succeeded ===", run_id)
            _maybe_set_run_terminated(active_run, "FINISHED")
        else:
            _maybe_set_run_terminated(active_run, "FAILED")
            raise ExecutionException("Run (ID '%s') failed" % run_id)
    except KeyboardInterrupt:
        _logger.error("=== Run (ID '%s') interrupted, cancelling run ===", run_id)
        submitted_run_obj.cancel()
        _maybe_set_run_terminated(active_run, "FAILED")
        raise


def _parse_subdirectory(uri):
    # Parses a uri and returns the uri and subdirectory as separate values.
    # Uses '#' as a delimiter.
    subdirectory = ''
    parsed_uri = uri
    if '#' in uri:
        subdirectory = uri[uri.find('#') + 1:]
        parsed_uri = uri[:uri.find('#')]
    if subdirectory and '.' in subdirectory:
        raise ExecutionException("'.' is not allowed in project subdirectory paths.")
    return parsed_uri, subdirectory


def _get_storage_dir(storage_dir):
    if storage_dir is not None and not os.path.exists(storage_dir):
        os.makedirs(storage_dir)
    return tempfile.mkdtemp(dir=storage_dir)


def _get_git_repo_url(work_dir):
    from git import Repo
    from git.exc import GitCommandError, InvalidGitRepositoryError
    try:
        repo = Repo(work_dir, search_parent_directories=True)
        remote_urls = [remote.url for remote in repo.remotes]
        if len(remote_urls) == 0:
            return None
    except GitCommandError:
        return None
    except InvalidGitRepositoryError:
        return None
    return remote_urls[0]


def _expand_uri(uri):
    if _is_local_uri(uri):
        return os.path.abspath(uri)
    return uri


def _is_file_uri(uri):
    """Returns True if the passed-in URI is a file:// URI."""
    return _FILE_URI_REGEX.match(uri)


def _is_local_uri(uri):
    """Returns True if the passed-in URI should be interpreted as a path on the local filesystem."""
    return not _GIT_URI_REGEX.match(uri)


def _is_zip_uri(uri):
    """Returns True if the passed-in URI points to a ZIP file."""
    return _ZIP_URI_REGEX.match(uri)


def _is_valid_branch_name(work_dir, version):
    """
    Returns True if the ``version`` is the name of a branch in a Git project.
    ``work_dir`` must be the working directory in a git repo.
    """
    if version is not None:
        from git import Repo
        from git.exc import GitCommandError
        repo = Repo(work_dir, search_parent_directories=True)
        try:
            return repo.git.rev_parse("--verify", "refs/heads/%s" % version) is not ''
        except GitCommandError:
            return False
    return False


def _fetch_project(uri, force_tempdir, version=None):
    """
    Fetch a project into a local directory, returning the path to the local project directory.
    :param force_tempdir: If True, will fetch the project into a temporary directory. Otherwise,
                          will fetch ZIP or Git projects into a temporary directory but simply
                          return the path of local projects (i.e. perform a no-op for local
                          projects).
    """
    parsed_uri, subdirectory = _parse_subdirectory(uri)
    use_temp_dst_dir = force_tempdir or _is_zip_uri(parsed_uri) or not _is_local_uri(parsed_uri)
    dst_dir = tempfile.mkdtemp() if use_temp_dst_dir else parsed_uri
    if use_temp_dst_dir:
        _logger.info("=== Fetching project from %s into %s ===", uri, dst_dir)
    if _is_zip_uri(parsed_uri):
        if _is_file_uri(parsed_uri):
            from six.moves import urllib
            parsed_file_uri = urllib.parse.urlparse(urllib.parse.unquote(parsed_uri))
            parsed_uri = os.path.join(parsed_file_uri.netloc, parsed_file_uri.path)
        _unzip_repo(zip_file=(
            parsed_uri if _is_local_uri(parsed_uri) else _fetch_zip_repo(parsed_uri)),
            dst_dir=dst_dir)
    elif _is_local_uri(uri):
        if version is not None:
            raise ExecutionException("Setting a version is only supported for Git project URIs")
        if use_temp_dst_dir:
            dir_util.copy_tree(src=parsed_uri, dst=dst_dir)
    else:
        assert _GIT_URI_REGEX.match(parsed_uri), "Non-local URI %s should be a Git URI" % parsed_uri
        _fetch_git_repo(parsed_uri, version, dst_dir)
    res = os.path.abspath(os.path.join(dst_dir, subdirectory))
    if not os.path.exists(res):
        raise ExecutionException("Could not find subdirectory %s of %s" % (subdirectory, dst_dir))
    return res


def _unzip_repo(zip_file, dst_dir):
    import zipfile
    with zipfile.ZipFile(zip_file) as zip_in:
        zip_in.extractall(dst_dir)


def _fetch_zip_repo(uri):
    import requests
    from io import BytesIO
    # TODO (dbczumar): Replace HTTP resolution via ``requests.get`` with an invocation of
    # ```mlflow.data.download_uri()`` when the API supports the same set of available stores as
    # the artifact repository (Azure, FTP, etc). See the following issue:
    # https://github.com/mlflow/mlflow/issues/763.
    response = requests.get(uri)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise ExecutionException("Unable to retrieve ZIP file. Reason: %s" % str(error))
    return BytesIO(response.content)


def _fetch_git_repo(uri, version, dst_dir):
    """
    Clone the git repo at ``uri`` into ``dst_dir``, checking out commit ``version`` (or defaulting
    to the head commit of the repository's master branch if version is unspecified).
    Assumes authentication parameters are specified by the environment, e.g. by a Git credential
    helper.
    """
    # We defer importing git until the last moment, because the import requires that the git
    # executable is availble on the PATH, so we only want to fail if we actually need it.
    import git
    repo = git.Repo.init(dst_dir)
    origin = repo.create_remote("origin", uri)
    origin.fetch()
    if version is not None:
        try:
            repo.git.checkout(version)
        except git.exc.GitCommandError as e:
            raise ExecutionException("Unable to checkout version '%s' of git repo %s"
                                     "- please ensure that the version exists in the repo. "
                                     "Error: %s" % (version, uri, e))
    else:
        repo.create_head("master", origin.refs.master)
        repo.heads.master.checkout()


def _get_conda_env_name(conda_env_path, env_id=None):
    conda_env_contents = open(conda_env_path).read() if conda_env_path else ""
    if env_id:
        conda_env_contents += env_id
    return "mlflow-%s" % hashlib.sha1(conda_env_contents.encode("utf-8")).hexdigest()


def _get_conda_bin_executable(executable_name):
    """
    Return path to the specified executable, assumed to be discoverable within the 'bin'
    subdirectory of a conda installation.

    The conda home directory (expected to contain a 'bin' subdirectory) is configurable via the
    ``mlflow.projects.MLFLOW_CONDA_HOME`` environment variable. If
    ``mlflow.projects.MLFLOW_CONDA_HOME`` is unspecified, this method simply returns the passed-in
    executable name.
    """
    conda_home = os.environ.get(MLFLOW_CONDA_HOME)
    if conda_home:
        return os.path.join(conda_home, "bin/%s" % executable_name)
    return executable_name


def _get_or_create_conda_env(conda_env_path, env_id=None):
    """
    Given a `Project`, creates a conda environment containing the project's dependencies if such a
    conda environment doesn't already exist. Returns the name of the conda environment.
    :param conda_env_path: Path to a conda yaml file.
    :param env_id: Optional string that is added to the contents of the yaml file before
                   calculating the hash. It can be used to distinguish environments that have the
                   same conda dependencies but are supposed to be different based on the context.
                   For example, when serving the model we may install additional dependencies to the
                   environment after the environment has been activated.
    """
    conda_path = _get_conda_bin_executable("conda")
    try:
        process.exec_cmd([conda_path, "--help"], throw_on_error=False)
    except EnvironmentError:
        raise ExecutionException("Could not find Conda executable at {0}. "
                                 "Ensure Conda is installed as per the instructions "
                                 "at https://conda.io/docs/user-guide/install/index.html. You can "
                                 "also configure MLflow to look for a specific Conda executable "
                                 "by setting the {1} environment variable to the path of the Conda "
                                 "executable".format(conda_path, MLFLOW_CONDA_HOME))
    (_, stdout, _) = process.exec_cmd([conda_path, "env", "list", "--json"])
    env_names = [os.path.basename(env) for env in json.loads(stdout)['envs']]
    project_env_name = _get_conda_env_name(conda_env_path, env_id)
    if project_env_name not in env_names:
        _logger.info('=== Creating conda environment %s ===', project_env_name)
        if conda_env_path:
            process.exec_cmd([conda_path, "env", "create", "-n", project_env_name, "--file",
                              conda_env_path], stream_output=True)
        else:
            process.exec_cmd(
                [conda_path, "create", "-n", project_env_name, "python"], stream_output=True)
    return project_env_name


def _maybe_set_run_terminated(active_run, status):
    """
    If the passed-in active run is defined and still running (i.e. hasn't already been terminated
    within user code), mark it as terminated with the passed-in status.
    """
    if active_run is None:
        return
    run_id = active_run.info.run_id
    cur_status = tracking.MlflowClient().get_run(run_id).info.status
    if RunStatus.is_terminated(cur_status):
        return
    tracking.MlflowClient().set_terminated(run_id, status)


def _get_entry_point_command(project, entry_point, parameters, storage_dir):
    """
    Returns the shell command to execute in order to run the specified entry point.
    :param project: Project containing the target entry point
    :param entry_point: Entry point to run
    :param parameters: Parameters (dictionary) for the entry point command
    :param storage_dir: Base local directory to use for downloading remote artifacts passed to
                        arguments of type 'path'. If None, a temporary base directory is used.
    """
    storage_dir_for_run = _get_storage_dir(storage_dir)
    _logger.info(
        "=== Created directory %s for downloading remote URIs passed to arguments of"
        " type 'path' ===",
        storage_dir_for_run)
    commands = []
    commands.append(
        project.get_entry_point(entry_point).compute_command(parameters, storage_dir_for_run))
    return commands


def _run_entry_point(command, work_dir, experiment_id, run_id):
    """
    Run an entry point command in a subprocess, returning a SubmittedRun that can be used to
    query the run's status.
    :param command: Entry point command to run
    :param work_dir: Working directory in which to run the command
    :param run_id: MLflow run ID associated with the entry point execution.
    """
    env = os.environ.copy()
    env.update(_get_run_env_vars(run_id, experiment_id))
    _logger.info("=== Running command '%s' in run with ID '%s' === ", command, run_id)
    # in case os name is not 'nt', we are not running on windows. It introduces
    # bash command otherwise.
    if os.name != "nt":
        process = subprocess.Popen(["bash", "-c", command], close_fds=True, cwd=work_dir, env=env)
    else:
        process = subprocess.Popen(command, close_fds=True, cwd=work_dir, env=env)
    return LocalSubmittedRun(run_id, process)


def _build_mlflow_run_cmd(
        uri, entry_point, storage_dir, use_conda, run_id, parameters):
    """
    Build and return an array containing an ``mlflow run`` command that can be invoked to locally
    run the project at the specified URI.
    """
    mlflow_run_arr = ["mlflow", "run", uri, "-e", entry_point, "--run-id", run_id]
    if storage_dir is not None:
        mlflow_run_arr.extend(["--storage-dir", storage_dir])
    if not use_conda:
        mlflow_run_arr.append("--no-conda")
    for key, value in parameters.items():
        mlflow_run_arr.extend(["-P", "%s=%s" % (key, value)])
    return mlflow_run_arr


def _run_mlflow_run_cmd(mlflow_run_arr, env_map):
    """
    Invoke ``mlflow run`` in a subprocess, which in turn runs the entry point in a child process.
    Returns a handle to the subprocess. Popen launched to invoke ``mlflow run``.
    """
    final_env = os.environ.copy()
    final_env.update(env_map)
    # Launch `mlflow run` command as the leader of its own process group so that we can do a
    # best-effort cleanup of all its descendant processes if needed
    if sys.platform == "win32":
        return subprocess.Popen(
            mlflow_run_arr, env=final_env, universal_newlines=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        return subprocess.Popen(
            mlflow_run_arr, env=final_env, universal_newlines=True, preexec_fn=os.setsid)


def _create_run(uri, experiment_id, work_dir, entry_point):
    """
    Create a ``Run`` against the current MLflow tracking server, logging metadata (e.g. the URI,
    entry point, and parameters of the project) about the run. Return an ``ActiveRun`` that can be
    used to report additional data about the run (metrics/params) to the tracking server.
    """
    if _is_local_uri(uri):
        source_name = tracking.utils._get_git_url_if_present(_expand_uri(uri))
    else:
        source_name = _expand_uri(uri)
    source_version = _get_git_commit(work_dir)
    existing_run = fluent.active_run()
    if existing_run:
        parent_run_id = existing_run.info.run_id
    else:
        parent_run_id = None

    tags = {
        MLFLOW_USER: context._get_user(),
        MLFLOW_SOURCE_NAME: source_name,
        MLFLOW_SOURCE_TYPE: SourceType.to_string(SourceType.PROJECT),
        MLFLOW_PROJECT_ENTRY_POINT: entry_point
    }
    if source_version is not None:
        tags[MLFLOW_GIT_COMMIT] = source_version
    if parent_run_id is not None:
        tags[MLFLOW_PARENT_RUN_ID] = parent_run_id

    active_run = tracking.MlflowClient().create_run(experiment_id=experiment_id, tags=tags)
    return active_run


def _get_run_env_vars(run_id, experiment_id):
    """
    Returns a dictionary of environment variable key-value pairs to set in subprocess launched
    to run MLflow projects.
    """
    return {
        tracking._RUN_ID_ENV_VAR: run_id,
        tracking._TRACKING_URI_ENV_VAR: tracking.get_tracking_uri(),
        tracking._EXPERIMENT_ID_ENV_VAR: str(experiment_id),
    }


def _invoke_mlflow_run_subprocess(
        work_dir, entry_point, parameters, experiment_id, use_conda, storage_dir, run_id):
    """
    Run an MLflow project asynchronously by invoking ``mlflow run`` in a subprocess, returning
    a SubmittedRun that can be used to query run status.
    """
    _logger.info("=== Asynchronously launching MLflow run with ID %s ===", run_id)
    mlflow_run_arr = _build_mlflow_run_cmd(
        uri=work_dir, entry_point=entry_point, storage_dir=storage_dir, use_conda=use_conda,
        run_id=run_id, parameters=parameters)
    mlflow_run_subprocess = _run_mlflow_run_cmd(
        mlflow_run_arr, _get_run_env_vars(run_id, experiment_id))
    return LocalSubmittedRun(run_id, mlflow_run_subprocess)


def _get_conda_command(conda_env_name):
    activate_path = _get_conda_bin_executable("activate")
    # in case os name is not 'nt', we are not running on windows. It introduces
    # bash command otherwise.
    if os.name != "nt":
        return ["source %s %s" % (activate_path, conda_env_name)]
    else:
        return ["conda %s %s" % (activate_path, conda_env_name)]


def _validate_execution_environment(project, backend):
    if project.docker_env and backend == "databricks":
        raise ExecutionException(
            "Running docker-based projects on Databricks is not yet supported.")


def _get_docker_command(image, active_run):
    docker_path = "docker"
    cmd = [docker_path, "run", "--rm"]
    env_vars = _get_run_env_vars(run_id=active_run.info.run_id,
                                 experiment_id=active_run.info.experiment_id)
    tracking_uri = tracking.get_tracking_uri()
    if tracking.utils._is_local_uri(tracking_uri):
        path = file_utils.local_file_uri_to_path(tracking_uri)
        cmd += ["-v", "%s:%s" % (path, _MLFLOW_DOCKER_TRACKING_DIR_PATH)]
        env_vars[tracking._TRACKING_URI_ENV_VAR] = _MLFLOW_DOCKER_TRACKING_DIR_PATH
    if tracking.utils._is_databricks_uri(tracking_uri):
        db_profile = mlflow.tracking.utils.get_db_profile_from_uri(tracking_uri)
        config = databricks_utils.get_databricks_host_creds(db_profile)
        # We set these via environment variables so that only the current profile is exposed, rather
        # than all profiles in ~/.databrickscfg; maybe better would be to mount the necessary
        # part of ~/.databrickscfg into the container
        env_vars[tracking._TRACKING_URI_ENV_VAR] = 'databricks'
        env_vars['DATABRICKS_HOST'] = config.host
        if config.username:
            env_vars['DATABRICKS_USERNAME'] = config.username
        if config.password:
            env_vars['DATABRICKS_PASSWORD'] = config.password
        if config.token:
            env_vars['DATABRICKS_TOKEN'] = config.token
        if config.ignore_tls_verification:
            env_vars['DATABRICKS_INSECURE'] = config.ignore_tls_verification

    for key, value in env_vars.items():
        cmd += ["-e", "{key}={value}".format(key=key, value=value)]
    cmd += [image]
    return cmd


def _validate_docker_installation():
    """
    Verify if Docker is installed on host machine.
    """
    try:
        docker_path = "docker"
        process.exec_cmd([docker_path, "--help"], throw_on_error=False)
    except EnvironmentError:
        raise ExecutionException("Could not find Docker executable. "
                                 "Ensure Docker is installed as per the instructions "
                                 "at https://docs.docker.com/install/overview/.")


def _validate_docker_env(docker_env):
    if not docker_env.get('image'):
        raise ExecutionException("Project with docker environment must specify the docker image "
                                 "to use via an 'image' field under the 'docker_env' field")


def _create_docker_build_ctx(work_dir, dockerfile_contents):
    """
    Creates build context tarfile containing Dockerfile and project code, returning path to tarfile
    """
    directory = tempfile.mkdtemp()
    try:
        dst_path = os.path.join(directory, "mlflow-project-contents")
        shutil.copytree(src=work_dir, dst=dst_path)
        with open(os.path.join(dst_path, _GENERATED_DOCKERFILE_NAME), "w") as handle:
            handle.write(dockerfile_contents)
        _, result_path = tempfile.mkstemp()
        file_utils.make_tarfile(
            output_filename=result_path,
            source_dir=dst_path, archive_name=_PROJECT_TAR_ARCHIVE_NAME)
    finally:
        shutil.rmtree(directory)
    return result_path


def _build_docker_image(work_dir, project, active_run):
    """
    Build a docker image containing the project in `work_dir`, using the base image and tagging the
    built image with the project name specified by `project`.
    """
    if not project.name:
        raise ExecutionException("Project name in MLproject must be specified when using docker "
                                 "for image tagging.")
    tag_name = "mlflow-{name}-{version}".format(
        name=(project.name if project.name else "docker-project"),
        version=_get_git_commit(work_dir)[:7], )
    dockerfile = (
        "FROM {imagename}\n"
        "LABEL Name={tag_name}\n"
        "COPY {build_context_path}/* /mlflow/projects/code/\n"
        "WORKDIR /mlflow/projects/code/\n"
    ).format(imagename=project.docker_env.get('image'), tag_name=tag_name,
             build_context_path=_PROJECT_TAR_ARCHIVE_NAME)
    build_ctx_path = _create_docker_build_ctx(work_dir, dockerfile)
    with open(build_ctx_path, 'rb') as docker_build_ctx:
        _logger.info("=== Building docker image %s ===", tag_name)
        client = docker.from_env()
        image = client.images.build(
            tag=tag_name, forcerm=True,
            dockerfile=posixpath.join(_PROJECT_TAR_ARCHIVE_NAME, _GENERATED_DOCKERFILE_NAME),
            fileobj=docker_build_ctx, custom_context=True, encoding="gzip")
    try:
        os.remove(build_ctx_path)
    except Exception:  # pylint: disable=broad-except
        _logger.info("Temporary docker context file %s was not deleted.", build_ctx_path)
    tracking.MlflowClient().set_tag(active_run.info.run_id,

                                    MLFLOW_DOCKER_IMAGE_NAME,
                                    tag_name)
    tracking.MlflowClient().set_tag(active_run.info.run_id,
                                    MLFLOW_DOCKER_IMAGE_ID,
                                    image[0].id)
    return tag_name


__all__ = [
    "run",
    "SubmittedRun"
]