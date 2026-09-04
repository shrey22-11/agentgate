"""
Defensive AI layer (Phases 9-10).

The LLM understands, structures, and shops. It never decides a verdict,
computes a discount ceiling or counter-offer, or moves money — structured
results are re-validated and routed through the same deterministic policy path
as POST /actions.

Public surface:
    parse_natural_language_action(session, client, *, agent_id, raw_input)
    run_buyer_agent(session, client, *, agent_id, goal)
    get_ai_client() / get_ai_buyer_client()      FastAPI dependencies
    AIParserClient / GeminiParserClient / DisabledAIClient
    AIBuyerClient / GeminiBuyerClient / DisabledBuyerClient
    AIError / AIDisabledError / AIUnavailableError
    ParsedIntent / NLActionRequest / NLActionResponse
    BuyerRunRequest / BuyerRunResponse / BuyerStep / BuyerToolCall
"""
from app.ai.buyer import run_buyer_agent
from app.ai.client import (
    AIBuyerClient,
    AIDisabledError,
    AIError,
    AIParserClient,
    AIUnavailableError,
    BuyerStep,
    BuyerToolCall,
    DisabledAIClient,
    DisabledBuyerClient,
    GeminiBuyerClient,
    GeminiParserClient,
    get_ai_buyer_client,
    get_ai_client,
)
from app.ai.parser import parse_natural_language_action
from app.ai.schemas import (
    BuyerRunRequest,
    BuyerRunResponse,
    NLActionRequest,
    NLActionResponse,
    ParsedIntent,
)

__all__ = [
    "parse_natural_language_action",
    "run_buyer_agent",
    "get_ai_client",
    "get_ai_buyer_client",
    "AIParserClient",
    "GeminiParserClient",
    "DisabledAIClient",
    "AIBuyerClient",
    "GeminiBuyerClient",
    "DisabledBuyerClient",
    "BuyerStep",
    "BuyerToolCall",
    "AIError",
    "AIDisabledError",
    "AIUnavailableError",
    "ParsedIntent",
    "NLActionRequest",
    "NLActionResponse",
    "BuyerRunRequest",
    "BuyerRunResponse",
]
