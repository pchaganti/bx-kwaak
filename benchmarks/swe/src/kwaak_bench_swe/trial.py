"""Trial execution and result management for SWE-bench evaluation.

This module provides classes for executing and evaluating individual test trials:
- TrialResult: Represents the outcome of a trial execution
- Trial: Manages the execution of a single test case

The module handles:
- Docker container lifecycle
- Test environment setup
- Patch application and validation
- Test execution and result collection
- Result evaluation and grading

Typical usage:
    trial = Trial(instance, "test-1", "./results")
    result = trial.run()
    if result.success:
        print("Test passed!")
"""

import logging
import os
import json
import subprocess
from dataclasses import dataclass, asdict
from typing import Any

from .swe_bench_instance import SWEBenchInstance
from .docker_instance import DockerInstance

from swebench.harness.grading import get_eval_report
from swebench.harness.test_spec.test_spec import (
    make_test_spec,
    TestSpec,
)

from swebench.harness.constants import (
    START_TEST_OUTPUT,
    END_TEST_OUTPUT,
)


@dataclass
class TrialResult:
    """Represents the outcome of a trial execution.

    This class encapsulates all information about a trial's execution,
    including success/failure status, error messages, and the generated patch.

    Attributes:
        instance: The SWE-bench instance that was tested
        run_failed: Whether there was an error during execution
        validation_failed: Whether the test validation failed
        success: Whether the trial successfully passed all tests
        error: Error message if any failure occurred
        patch: The patch generated by the agent
    """

    instance: SWEBenchInstance
    run_failed: bool = False
    validation_failed: bool = False
    success: bool = False
    error: str | None = None
    patch: str | None = None

    def failed(self) -> bool:
        """Check if the trial failed in any way.

        Returns:
            bool: True if there was any kind of failure (run, validation, or error),
                  False if the trial completed successfully
        """
        return self.run_failed or self.validation_failed or (self.error is not None)

    def to_dict(self) -> dict[str, Any]:
        """Convert the TrialResult to a dictionary for JSON serialization.

        This method handles nested objects by checking for and using their
        to_dict methods when available.

        Returns:
            dict[str, Any]: A JSON-serializable dictionary containing all trial
                           result data, including nested objects
        """
        result = asdict(self)
        # Convert the SWEBenchInstance to a dict if it has a to_dict method
        if hasattr(self.instance, "to_dict"):
            result["instance"] = self.instance.to_dict()
        return result


class Trial:
    """Manages the execution of a single SWE-bench test case.

    This class handles the complete lifecycle of a test trial, including:
    - Docker container setup and cleanup
    - Test environment preparation
    - Patch application and validation
    - Test execution and result collection
    - Result evaluation and grading

    The class integrates with the SWE-bench evaluation framework to ensure
    consistent test execution and result reporting.
    """

    item: SWEBenchInstance
    name: str
    container: DockerInstance
    results_dir: str

    def __init__(self, item: SWEBenchInstance, name: str, results_dir: str) -> None:
        """Initialize a new trial.

        Args:
            item: The SWE-bench instance to test
            name: Unique identifier for this trial
            results_dir: Directory where results and artifacts will be stored
        """
        self.item = item
        self.name = name
        self.results_dir = results_dir
        self.container = DockerInstance(self.item, self.results_dir)

    def run(self) -> TrialResult:
        """Execute the trial.

        This method performs the complete trial execution:
        1. Sets up the Docker container
        2. Applies the test patch
        3. Establishes initial git state
        4. Runs pre-patch tests
        5. Installs and runs the agent
        6. Collects and evaluates results

        Returns:
            TrialResult: The complete results of the trial execution
        """
        logging.info(f"Running trial {self.name}")

        try:
            self.container.run(self.name)

            # Write the test_cmd to a shell script
            test_cmd_path = "/swe/test.sh"
            test_cmd = f"#!/bin/bash\nset -e\n{self.item.test_cmd}\n"
            self.container.write_string_to_file(test_cmd, test_cmd_path)
            self.container.exec(f"chmod +x {test_cmd_path}")

            # Then apply the patch
            self.container.write_string_to_file(self.item.test_patch, "/swe/test.patch")
            # Try to apply the patch and get detailed error if it fails
            patch_result = self.container.exec("git apply /swe/test.patch")
            if patch_result.exit_code != 0:
                # Get more details about the failure
                logging.info(f"Test Patch failed with code {patch_result.exit_code}")
                logging.info(f"Patch output: {patch_result.output}")

                # Try with -v for more details
                verbose_result = self.container.exec("git apply -v /swe/test.patch")
                logging.info(f"Verbose patch output: {verbose_result.output}")

                return TrialResult(
                    instance=self.item,
                    run_failed=False,
                    validation_failed=True,
                    error=f"Patch failed: {patch_result.output.decode()}",
                )

            # Establish initial git state
            initial_git_ref = self.establish_initial_git_ref()

            pre_patch_results = self.container.exec("/swe/test.sh")
            pre_patch_results_path = os.path.join(
                self.results_dir, f"pre_patch_test_results.txt"
            )

            # write results to file in results_dir
            with open(pre_patch_results_path, "w") as f:
                f.write(pre_patch_results.output.decode())

            # Run the agent
            self.install_agent()
            self.run_agent()

            # Get the changes made by the agent
            diff = self.container.exec(
                f"git diff {initial_git_ref} HEAD"
            ).output.decode()

            prediction = {
                "instance_id": self.item.instance_id,
                "model_name_or_path": self.name,
                "model_patch": diff,
            }

            prediction_path = os.path.join(self.results_dir, f"prediction.json")
            with open(prediction_path, "w") as f:
                json.dump(prediction, f, indent=2)

            test_results = self.container.exec("/swe/test.sh").output.decode()
            test_results_path = os.path.join(self.results_dir, f"test_results.txt")

            with open(test_results_path, "w") as f:
                f.write(f"{START_TEST_OUTPUT}\n")
                f.write(test_results)
                f.write(f"\n{END_TEST_OUTPUT}\n")

            model_patch_path = os.path.join(self.results_dir, f"patch.diff")

            with open(model_patch_path, "w") as f:
                f.write(diff)

            result = self.evaluate_results(prediction, test_results_path)

            # TODO: Uncomment next line when debugging is done:
            # self.container.cleanup()

            return result
        except Exception as e:
            return TrialResult(
                instance=self.item,
                run_failed=True,
                validation_failed=False,
                error=str(e),
            )

    def establish_initial_git_ref(self) -> str:
        """Create a git commit of the current state and get its reference.

        This method:
        1. Configures git user information
        2. Creates a commit with the current state
        3. Returns the commit hash

        Returns:
            str: The hash of the created commit

        Raises:
            Exception: If git operations fail
        """
        # Configure git user
        result = self.container.exec("git config user.name 'agent-test-harness'")
        if result.exit_code != 0:
            raise Exception(f"Failed to configure git user name: {result.output}")

        result = self.container.exec(
            "git config user.email 'agent-test-harness@bosun.ai'"
        )
        if result.exit_code != 0:
            raise Exception(f"Failed to configure git user email: {result.output}")

        # Create initial commit - git commit may return non-zero even on success
        self.container.exec("git add .")
        result = self.container.exec("git commit -a -m 'benchmark-head'")
        if result.exit_code != 0:
            logging.info(
                f"Failed to create initial commit (exit code: {result.exit_code}): {result.output}"
            )

        # Get commit hash - this will fail if there really was no commit
        result = self.container.exec("git rev-parse HEAD")
        if result.exit_code != 0:
            raise Exception(f"Failed to get commit hash: {result.output}")

        return result.output.decode().strip()

    def install_agent(self) -> None:
        """Install the Kwaak agent in the test environment.

        This method:
        1. Locates the agent source in the git repository
        2. Ensures cross-compilation tools are available
        3. Builds the agent for x86_64 Linux
        4. Copies the binary to the container directory

        Raises:
            subprocess.CalledProcessError: If any build step fails
        """
        # agent is located at the root of the git repository of the cwd
        agent_root = (
            subprocess.check_output(["git", "rev-parse", "--show-toplevel"])
            .decode()
            .strip()
        )

        # check if cross is installed
        if subprocess.run(["cross", "--version"], check=False).returncode != 0:
            subprocess.run(
                [
                    "cargo",
                    "install",
                    "cross",
                    "--git",
                    "https://github.com/cross-rs/cross",
                ],
                check=True,
            )

        # copy the agent binary to the root of the results directory
        agent_path = os.path.join(
            agent_root, "target", "x86_64-unknown-linux-gnu", "release", "kwaak"
        )

        if not os.path.exists(agent_path):
            # we use cargo build to ensure the agent is built for the x96_64 architecture
            logging.info(f"Building agent in {agent_root}")
            subprocess.run(
                ["cross", "build", "--target", "x86_64-unknown-linux-gnu", "--release"],
                cwd=agent_root,
            )

        self.container.exec("apt-get update")
        self.container.exec("apt-get install -y ripgrep fd-find")

        subprocess.run(["cp", agent_path, self.container.instance_dir])
        self.container.exec("chmod +x /swe/kwaak")
        logging.info("Copying agent to container")
        self.container.exec("cp /swe/kwaak /usr/local/bin/kwaak")

        # write kwaak execution script to container
        kwaak_script = """#!/bin/bash
      echo "Setting modes.."
      set -e
      set -x
      echo "Linking fdfind to fd"
      ln -s $(which fdfind) /usr/local/bin/fd
      echo "Dumping env.."
      env > /swe/env.log
      echo "Invoking kwaak.."
      kwaak --config-path /swe/kwaak.rendered.toml run-agent --initial-message "$PROMPT" 2>&1 | tee /swe/kwaak.log
    """
        self.container.write_string_to_file(kwaak_script, "/swe/kwaak.sh")
        self.container.exec("chmod +x /swe/kwaak.sh")

    def run_agent(self) -> None:
        """Execute the Kwaak agent in the test environment.

        This method:
        1. Sets up the agent configuration
        2. Runs the agent in run-agent mode
        3. Provides the problem statement as initial message

        """
        template_path = os.path.join(os.path.dirname(__file__), "kwaak.template.toml")
        with open(template_path, "r") as f:
            template = f.read()

        template_path = os.path.join(self.container.instance_dir, "kwaak.rendered.toml")
        with open(template_path, "w") as f:
            f.write(template)

        self.invoke_kwaak()

    def invoke_kwaak(self):
        import threading
        import queue
        import time

        logging.info("Invoking kwaak")
        openai_api_key = os.environ["OPENAI_API_KEY"]
        prompt = self.render_prompt()

        # Queue to store the result from the thread
        result_queue = queue.Queue()

        # Split on "/" then get tail
        project_name = self.item.repo.split("/")[-1]

        def run_kwaak():
            try:
                result = self.container.exec(
                    "/swe/kwaak.sh",
                    env={
                        "PROMPT": prompt,
                        "OPENAI_API_KEY": openai_api_key,
                        "RUST_LOG": "debug",
                        "RUST_BACKTRACE": "1",
                        "KWAAK__PROJECT_NAME": project_name,
                    },
                )
                result_queue.put(result)
            except Exception as e:
                result_queue.put(e)

        # Start the thread
        thread = threading.Thread(target=run_kwaak)
        thread.daemon = (
            True  # Make thread daemon so it will be killed when main thread exits
        )
        thread.start()

        # Wait for the thread with timeout
        minutes = 60
        timeout = minutes * 60
        timeout_time = time.time() + timeout

        agent_result_path = os.path.join(self.results_dir, "agent_result.txt")

        try:
            # Wait for the thread to complete or timeout
            while thread.is_alive() and time.time() < timeout_time:
                thread.join(1.0)  # Check every second

            if thread.is_alive():
                # Timeout occurred
                with open(agent_result_path, "w") as f:
                    f.write(f"Timeout Error {minutes} minutes")
                return

            # Get the result if thread completed
            result = result_queue.get_nowait()
            if isinstance(result, Exception):
                # Handle any exceptions that occurred in the thread
                with open(agent_result_path, "w") as f:
                    f.write(f"Error: {str(result)}")
            else:
                # Write successful result
                with open(agent_result_path, "w") as f:
                    f.write(result.output.decode())
                    f.write(f"\nExit Code: {result.exit_code}")

        except queue.Empty:
            # This shouldn't happen since we already checked thread.is_alive()
            with open(agent_result_path, "w") as f:
                f.write("Unexpected error: No result from thread")

    def render_prompt(self):
        return (
            "A user has reported the following issue:\n\n"
            f"<issue>\n{self.item.problem_statement}\n</issue>\n\n"
            "Could you solve the issue? I have added a failing test case for it. Using the following patch:\n\n"
            f"<patch>\n{self.item.test_patch}\n</patch>\n\n"
            "Please make sure that your solution makes the test(s) in this patch pass, "
            "and does not introduce any new failing tests. "
            "You can ignore tests that were already failing that are not related to the tests in this patch."
            "Do not modify the tests in this patch nor any other tests in the repository, only fix the issue."
        )

    def evaluate_results(self, prediction: dict, results_path: str) -> TrialResult:
        """Evaluate the trial results using SWE-bench grading.

        Args:
            prediction: Dictionary containing the agent's patch and metadata
            results: String output from the test execution

        Returns:
            TrialResult: Evaluation results including success status and patch

        This method:
        1. Prepares the test specification
        2. Gets the evaluation report from SWE-bench
        3. Determines if tests were resolved
        4. Saves the evaluation report
        """
        instance_id = self.item.instance_id

        test_spec = make_test_spec(self.item.to_dict())

        logging.info(f"test_spec: {test_spec}")
        report = get_eval_report(
            test_spec, prediction, results_path, include_tests_status=True
        )
        logging.info(f"report: {report}")
        resolved = report[instance_id]["resolved"]

        logging.info(
            f"report: {report}\nResult for {instance_id}: resolved: {resolved}"
        )

        report_path = os.path.join(self.results_dir, f"report.json")

        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        return TrialResult(
            instance=self.item,
            run_failed=False,
            validation_failed=False,
            patch=prediction["model_patch"],
            success=resolved,
            error=None,
        )
