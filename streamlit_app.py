import streamlit as st
import asyncio
import os
from teams.analyzer_gpt import GetDataAnalyzerTeam
from models.openai_model_client import get_model_client
from config.docker_util import getDockerCommandLineCodeExecutor, start_docker_container, stop_docker_container
from autogen_agentchat.messages import TextMessage

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
        with open('tmp/data.csv', 'wb') as f:
            f.write(uploaded_file.getvalue())
        pass
    else:
        st.warning("Please upload a CSV file and enter a task")
