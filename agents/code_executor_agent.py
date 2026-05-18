from autogen_agentchat.agents import CodeExecutorAgent
from config.docker_util import getDockerCommandLineCodeExecutor
from config.constants import WORK_DIR_DOCKER, TIMEOUT_DOCKER

def getCodeExecutorAgent(code_executor):
    code_executor_agent=CodeExecutorAgent(
        name="CodeExecutorAgent",
        code_executor=code_executor
    )
    return code_executor_agent
