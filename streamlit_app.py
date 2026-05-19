import streamlit as st
import asyncio
import os
from teams.analyzer_gpt import GetDataAnalyzerTeam
from models.openai_model_client import get_model_client
from config.docker_util import getDockerCommandLineCodeExecutor, start_docker_container, stop_docker_container
from autogen_agentchat.messages import TextMessage
from dotenv import load_dotenv
load_dotenv()

st.title("Analyser GPT - Digital Data Analyzer")
uploaded_file = st.file_uploader("Upload a CSV file", type=["csv"])

task=st.chat_input("Enter your text here...")

async def run_analyzer_gpt(docker, model_client, task):
    try:
        await start_docker_container(docker)
        team=GetDataAnalyzerTeam(docker, model_client)

        async for message in team.run_stream(task=task):
            st.markdown(f"**(message.source)**: {message.content}")
        return None

    except Exception as e:
        st.error(f"Error: {e}")
        return e

    finally:
        await stop_docker_container(docker)


if task:
    if uploaded_file is not None:
        if not os.path.exists('tmp'):
            os.makedirs('tmp')

        with open('tmp/data.csv', 'wb') as f:
            f.write(uploaded_file.getvalue())

        openai_model_client=get_model_client()
        docker=getDockerCommandLineCodeExecutor()

        error = asyncio.run(run_analyzer_gpt(docker, openai_model_client, task))
        if error:
            st.error(f"An error occurred: {error}")
        else:
            st.success("Analysis completed successfully")
    else:
        st.warning("Please upload a CSV file and enter a task")
