"""Translation of committed segments with the user's Hermes model."""

from hermeneus_engine.translate.languages import Language, resolve_target_language, supported_languages
from hermeneus_engine.translate.llm_translator import LlmTranslator
from hermeneus_engine.translate.prompt import ContextLine
from hermeneus_engine.translate.scheduler import TranslationScheduler

__all__ = ["ContextLine", "Language", "LlmTranslator", "TranslationScheduler", "resolve_target_language",
           "supported_languages"]
