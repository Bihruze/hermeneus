"""Translation of committed segments with the user's Hermes model."""

from .languages import Language, resolve_target_language, supported_languages
from .llm_translator import LlmTranslator
from .prompt import ContextLine
from .scheduler import TranslationScheduler

__all__ = ["ContextLine", "Language", "LlmTranslator", "TranslationScheduler", "resolve_target_language",
           "supported_languages"]
