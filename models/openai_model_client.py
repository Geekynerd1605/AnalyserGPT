from autogen_ext.models.openai import OpenAIChatCompletionModelClient
from config.constants import MODEL_NAME
import os

def get_model_client():
    openai_model_client=OpenAIChatCompletionModelClient(
        model=MODEL_NAME,
        api_key=os.getenv("OPENAI_API_KEY"),
    )
    return openai_model_client
