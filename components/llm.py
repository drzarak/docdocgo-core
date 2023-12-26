from typing import Any
from uuid import UUID

from langchain.callbacks.base import BaseCallbackHandler
from langchain.chat_models import AzureChatOpenAI, ChatOpenAI
from langchain.chat_models.base import BaseChatModel
from langchain.prompts.prompt import PromptTemplate
from langchain.prompts.chat import ChatPromptValue

# from langchain.schema import HumanMessage, AIMessage, SystemMessage
from langchain.schema import StrOutputParser
from langchain.schema.output import ChatGenerationChunk, GenerationChunk
from streamlit.delta_generator import DeltaGenerator

from utils.helpers import DELIMITER, MAIN_BOT_PREFIX
from utils.prepare import (
    CHAT_DEPLOYMENT_NAME,
    IS_AZURE,
    LLM_REQUEST_TIMEOUT,
)
from utils.type_utils import BotSettings, CallbacksOrNone
from utils.lang_utils import msg_list_chat_history_to_string


class CallbackHandlerDDGStreamlit(BaseCallbackHandler):
    def __init__(self, container: DeltaGenerator):
        self.container = container
        self.buffer = ""

    def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self.buffer += token.replace("\n", "  \n")
        self.container.markdown(self.buffer)


class CallbackHandlerDDGConsole(BaseCallbackHandler):
    def __init__(self, init_str: str = MAIN_BOT_PREFIX):
        self.init_str = init_str

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
    ) -> None:
        print(self.init_str, end="", flush=True)

    def on_llm_new_token(self, token, **kwargs) -> None:
        print(token, end="", flush=True)

    def on_llm_end(self, *args, **kwargs) -> None:
        print()

    def on_retry(self, *args, **kwargs) -> None:
        print(f"ON_RETRY: \nargs = {args}\nkwargs = {kwargs}")


def get_llm_with_callbacks(
    settings: BotSettings, callbacks: CallbacksOrNone = None
) -> BaseChatModel:
    """
    Returns a chat model instance (either AzureChatOpenAI or ChatOpenAI, depending
    on the value of IS_AZURE). In the case of AzureChatOpenAI, the model is
    determined by CHAT_DEPLOYMENT_NAME (and other Azure-specific environment variables),
    not by settings.model_name.
    """
    if IS_AZURE:
        llm = AzureChatOpenAI(
            deployment_name=CHAT_DEPLOYMENT_NAME,
            temperature=settings.temperature,
            request_timeout=LLM_REQUEST_TIMEOUT,
            streaming=True,  # seems to help with timeouts
            callbacks=callbacks,
        )
    else:
        llm = ChatOpenAI(
            model=settings.model_name,
            temperature=settings.temperature,
            request_timeout=LLM_REQUEST_TIMEOUT,
            streaming=True,
            callbacks=callbacks,
            verbose=True,  # tmp
        )
    return llm


def get_llm(
    settings: BotSettings,
    callbacks: CallbacksOrNone = None,
    stream=False,
    init_str=MAIN_BOT_PREFIX,
) -> BaseChatModel:
    """
    Return a chat model instance (either AzureChatOpenAI or ChatOpenAI, depending
    on the value of IS_AZURE). If callbacks is passed, it will be used as the
    callbacks for the chat model. Otherwise, if stream is True, then a CallbackHandlerDDG
    will be used with the passed init_str as the init_str.
    """
    if callbacks is None:
        callbacks = [CallbackHandlerDDGConsole(init_str)] if stream else []
    return get_llm_with_callbacks(settings, callbacks)


def get_prompt_llm_chain(
    prompt: PromptTemplate, llm_settings: BotSettings, print_prompt=False, **kwargs
):
    if not print_prompt:
        return prompt | get_llm(llm_settings, **kwargs) | StrOutputParser()

    def print_and_return(thing):
        if isinstance(thing, ChatPromptValue):
            print(f"PROMPT:\n{msg_list_chat_history_to_string(thing.messages)}")
        else:
            print(f"PROMPT:\n{type(thing)}\n{thing}")
        print(DELIMITER)
        return thing

    return (
        prompt | print_and_return | get_llm(llm_settings, **kwargs) | StrOutputParser()
    )


if __name__ == "__main__":
    # NOTE: Run this file as "python -m components.llm"
    x = CallbackHandlerDDGConsole("BOT: ")
