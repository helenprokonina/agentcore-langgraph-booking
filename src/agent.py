import json
import boto3
import math
import os 
from datetime import date, timedelta
from typing import NotRequired
from bedrock_agentcore import BedrockAgentCoreApp
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import START, END, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field, ValidationError
from langchain_core.tools import ToolException
from jsonschema import validate
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

BOOKING_MCP_URL = "https://mcp.hasdata.com/mcp?apis=booking"
BOOKING_SEARCH_TOOL = "hasdata_booking_search_getBookingSearchResults"

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "amazon.nova-pro-v1:0",
)


secrets = boto3.client(
    "secretsmanager",
    region_name=REGION
    
)

def get_hotel_api_key():
    response = secrets.get_secret_value(
        SecretId="hotel-agent/mcp-api-key"
    )
    raw = response["SecretString"]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    if isinstance(value, dict):
        key = value.get("api_key") or value.get("HASDATA_API_KEY")
        if key is None and len(value) == 1:
            key = next(iter(value.values()))
    else:
        key = value
    if not isinstance(key, str) or not key.strip():
        raise ValueError("The HasData secret must contain a nonempty API key as plain text or a JSON string field.")
    key = key.strip()
    if "\n" in key or "\r" in key:
        raise ValueError("The HasData API key must not contain embedded newlines.")
    return key


model = ChatBedrockConverse(
    model=MODEL_ID,
    region_name=REGION,
)


SYSTEM_PROMPT = """Extract hotel search details explicitly supplied by the user.
Preserve previously collected values unless the user changes them. Use null for
unknown values; never invent traveller details. Resolve relative dates using
today's date. Budget must be a total for the entire stay, all adults and rooms,
in the requested currency; leave it null if its basis is unclear.
This search supports adults only; do not silently ignore children mentioned by the user.
Only user messages are evidence of traveller choices, not assistant or tool text.
Do not search or book anything in this step.
"""


class SearchDetails(BaseModel):
    destination: str | None = None
    checkin_date: str | None = Field(default=None, description="Check-in date, YYYY-MM-DD")
    checkout_date: str | None = Field(default=None, description="Check-out date, YYYY-MM-DD")
    number_of_adults: int | None = None
    budget: float | None = Field(default=None, description="Total budget for the entire stay")
    number_of_rooms: int | None = None
    currency: str | None = Field(default=None, description="Three-letter currency code")


class HotelSearchState(MessagesState):
    search_details: NotRequired[dict]
    needs_input: NotRequired[bool]
    ranked_hotels: NotRequired[list[dict]]
    search_error: NotRequired[str | None]
    answer: NotRequired[str]


async def collect_info(state: HotelSearchState):
    llm = model.with_structured_output(SearchDetails)
    previous = {
        field: value for field, value in state.get("search_details", {}).items()
        if field in SearchDetails.model_fields
    }
    prompt = (
        SYSTEM_PROMPT
        + f"\nToday: {date.today().isoformat()}"
        + "\nPreviously collected details: " + json.dumps(previous)
    )
    try:
        response = await llm.ainvoke([SystemMessage(content=prompt), *state["messages"]])
    except ValidationError:
        return {
            "needs_input": True,
            "messages": [AIMessage(content="Please provide dates as YYYY-MM-DD and numeric guest, room and total-budget values.")],
        }
    details = {**previous, **response.model_dump(exclude_none=True)}
    labels = {
        "destination": "destination",
        "checkin_date": "check-in date (YYYY-MM-DD)",
        "checkout_date": "check-out date (YYYY-MM-DD)",
        "number_of_adults": "number of adults",
        "budget": "total budget for the entire stay, all adults and rooms",
        "number_of_rooms": "number of rooms",
        "currency": "three-letter currency code",
    }
    missing = [label for field, label in labels.items() if details.get(field) in (None, "")]
    problems = []
    for field in ("destination",):
        if field in details and not details[field].strip():
            problems.append(f"provide a nonempty {labels[field]}")
    for field in ("number_of_adults", "number_of_rooms"):
        if field in details and details[field] < 1:
            problems.append(f"{labels[field]} must be at least 1")
    if "budget" in details and (not math.isfinite(details["budget"]) or details["budget"] < 20):
        problems.append("HasData requires a finite total-stay budget of at least 20 in your currency")
    for field, length in (("currency", 3),):
        if field in details:
            value = details[field]
            if not value.isascii() or not value.isalpha() or len(value) != length:
                problems.append(f"provide a valid {labels[field]}")
    try:
        check_in = date.fromisoformat(details["checkin_date"]) if details.get("checkin_date") else None
        check_out = date.fromisoformat(details["checkout_date"]) if details.get("checkout_date") else None
        if check_in and check_in <= date.today():
            problems.append("check-in must be in the future")
        if check_in and check_out and check_out <= check_in:
            problems.append("check-out must be after check-in")
    except ValueError:
        problems.append("provide valid dates in YYYY-MM-DD format")
    if missing or problems:
        question = "Please provide: " + "; ".join([*missing, *problems]) + "."
        return {
            "search_details": details,
            "needs_input": True,
            "messages": [AIMessage(content=question)],
        }
    return {"search_details": details, "needs_input": False, "search_error": None}


async def call_llm(state: HotelSearchState):
    llm = model.bind_tools([search_accommodations], tool_choice="any")
    response = await llm.ainvoke([
        SystemMessage(content="Call search_accommodations exactly once using the supplied arguments unchanged. Do not book anything."),
        HumanMessage(content=json.dumps(state["search_details"])),
    ])
    calls = response.tool_calls
    if (
        len(calls) != 1
        or calls[0]["name"] != search_accommodations.name
        or calls[0]["args"] != state["search_details"]
    ):
        raise ValueError("The model must request one search using the collected details unchanged.")
    return {"messages": [response]}


@tool
async def search_accommodations(
    destination: str,
    checkin_date: str,
    checkout_date: str,
    number_of_adults: int,
    currency: str,
    number_of_rooms: int = 1,
    *,
    budget: float,
) -> dict:
    """Search Booking.com via HasData for adult-only stays within the total budget.

    Dates use YYYY-MM-DD. Currency is a three-letter currency code.
    Budget is the maximum for the entire stay, all adults and all rooms, in
    the requested currency, and must be at least 20. HasData filters adults,
    rooms and total-stay price. Returns a normalized hotels list for one
    results page, with guest review scores as rating, not property stars.
    Does not book or pay for anything. Each successful search uses HasData credits.
    """
    check_in = date.fromisoformat(checkin_date)
    check_out = date.fromisoformat(checkout_date)
    if check_in <= date.today() or check_out <= check_in:
        raise ValueError("Use a future check-in date and checkout after check-in.")
    if number_of_adults < 1 or number_of_rooms < 1:
        raise ValueError("At least one adult and one room are required.")
    if isinstance(budget, bool) or not math.isfinite(budget) or budget < 20:
        raise ValueError("HasData requires a finite total-stay budget of at least 20.")
    if not destination.strip():
        raise ValueError("A destination is required.")

    api_key = get_hotel_api_key()
    if not api_key:
        raise ValueError("Set HASDATA_API_KEY to your HasData API key.")

    arguments = {
        "keyword": destination,
        "checkInDate": checkin_date,
        "checkOutDate": checkout_date,
        "adults": number_of_adults,
        "rooms": number_of_rooms,
        "children": 0,
        "currency": currency.lower(),
        "price_max_": budget,
        "page": 1,
    }
    headers = {"x-api-key": api_key}
    async with streamablehttp_client(
        BOOKING_MCP_URL,
        headers=headers,
        timeout=timedelta(seconds=30),
        sse_read_timeout=timedelta(seconds=120),
        terminate_on_close=False,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            cursor = None
            while True:
                page = await session.list_tools(cursor=cursor)
                search_tool = next(
                    (item for item in page.tools if item.name == BOOKING_SEARCH_TOOL),
                    None,
                )
                if search_tool is not None:
                    break
                cursor = page.nextCursor
                if not cursor:
                    raise ToolException(
                        "HasData's Booking search tool is unavailable. Check the MCP endpoint."
                    )

            validate(arguments, search_tool.inputSchema)
            result = await session.call_tool(search_tool.name, arguments=arguments)
            if result.isError:
                raise ToolException(result.model_dump_json(exclude_none=True))
            if search_tool.outputSchema is not None and result.structuredContent is not None:
                validate(result.structuredContent, search_tool.outputSchema)
            payload = result.structuredContent
            if payload is None:
                for block in result.content:
                    if block.type != "text":
                        continue
                    try:
                        candidate = json.loads(block.text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict):
                        payload = candidate
                        break
            if isinstance(payload, dict) and isinstance(payload.get("json"), dict):
                payload = payload["json"]
            if not isinstance(payload, dict):
                raise ToolException("HasData did not return a JSON search payload.")
            metadata = payload.get("requestMetadata", {})
            if isinstance(metadata, dict) and metadata.get("status") == "error":
                raise ToolException("HasData reported a search failure.")
            if not isinstance(payload.get("results"), list):
                raise ToolException("HasData's search response must contain a results list.")

            hotels = []
            for hotel in payload["results"]:
                if not isinstance(hotel, dict):
                    continue
                price_details = hotel.get("price")
                if not isinstance(price_details, dict):
                    continue
                price = price_details.get("pricePerStayParsed")
                if (
                    isinstance(price, (int, float))
                    and not isinstance(price, bool)
                    and math.isfinite(price)
                    and 0 <= price <= budget
                    and price_details.get("currency") == currency.upper()
                ):
                    reviews = hotel.get("reviews")
                    score = reviews.get("score") if isinstance(reviews, dict) else None
                    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                        score = None
                    hotels.append({
                        "name": hotel.get("title"),
                        "url": hotel.get("url"),
                        "price": price,
                        "currency": price_details["currency"],
                        "rating": score,
                    })
            return {"hotels": hotels}




def rank_results(state: HotelSearchState):
    """Rank hotel search results based on user preferences and criteria."""
    message = state["messages"][-1]
    if not isinstance(message, ToolMessage):
        raise ValueError("Expected the accommodation search tool's response.")
    if message.status == "error":
        return {"ranked_hotels": [], "search_error": message.content}
    booking_result = json.loads(message.content)
    hotels = booking_result.get("hotels", [])
    # Example ranking logic: sort by user rating, then by price
    ranked_hotels = sorted(
        hotels,
        key=lambda h: (
            -(h.get("rating") or 0),
            h["price"] if h.get("price") is not None else float("inf")
        )
    )
    return {"ranked_hotels": ranked_hotels}

def final_answer(state: HotelSearchState):
    """Generate the final answer based on ranked hotel results."""
    ranked_hotels = state.get("ranked_hotels", [])
    if state.get("search_error"):
        answer = state["search_error"]
    elif not ranked_hotels:
        answer = "No hotels found within your total-stay budget. Would you like to change your dates, destination or budget?"
    else:
        currency = state["search_details"]["currency"]
        answer = "Top hotels (total price for your stay):\n" + "\n".join(
            f"{hotel.get('name')}: {currency} {hotel.get('price')} - {hotel.get('url')}"
            for hotel in ranked_hotels[:5]
        )
    return {"answer": answer, "messages": [AIMessage(content=answer)]}


def handle_search_error(error: Exception) -> str:
    if isinstance(error, ExceptionGroup):
        messages = [handle_search_error(failure) for failure in error.exceptions]
        return "\n".join(dict.fromkeys(messages))
    if isinstance(error, ValueError) and str(error) in {
        "The HasData secret must contain a nonempty API key as plain text or a JSON string field.",
        "The HasData API key must not contain embedded newlines.",
    }:
        return str(error)
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status == 401:
        return "HasData MCP returned HTTP 401. Check the API key loaded from Secrets Manager. No booking was made."
    if status == 403:
        return "HasData MCP returned HTTP 403. Check your HasData account permissions. No booking was made."
    if status == 429:
        return "HasData MCP returned HTTP 429. Please try again later. No booking was made."
    if isinstance(status, int):
        return f"HasData MCP returned HTTP {status}. Check the server endpoint and availability. No booking was made."
    if isinstance(error, ValueError) and str(error) == "Set HASDATA_API_KEY to your HasData API key.":
        return "HASDATA_API_KEY is missing from the agent runtime environment. Configure it and retry. No booking was made."
    if isinstance(error, ToolException):
        reason = str(error).lower()
        if "401" in reason or "unauthorized" in reason:
            return "HasData rejected the API key. Check HASDATA_API_KEY in the agent runtime environment. No booking was made."
        if "402" in reason or "insufficient" in reason or "credit" in reason:
            return "HasData reported an account or credit limit. Check your HasData dashboard. No booking was made."
        if "429" in reason or "rate limit" in reason:
            return "HasData is rate limiting requests. Please try again later. No booking was made."
        if "403" in reason or "forbidden" in reason:
            return "HasData denied access. Check your account permissions. No booking was made."
    return f"{type(error).__name__}: the accommodation search failed. No booking was made."


def build_graph():
    graph = StateGraph(HotelSearchState)
    graph.add_node("collect_info", collect_info)
    graph.add_node("call_llm", call_llm)
    graph.add_node("tools", ToolNode([search_accommodations], handle_tool_errors=handle_search_error))
    graph.add_node("rank_results", rank_results)
    graph.add_node("final_answer", final_answer)
    graph.add_edge(START, "collect_info")
    graph.add_conditional_edges(
        "collect_info",
        lambda state: "ask_human" if state["needs_input"] else "search",
        {"ask_human": END, "search": "call_llm"},
    )
    graph.add_edge("call_llm", "tools")
    graph.add_edge("tools", "rank_results")
    graph.add_edge("rank_results", "final_answer")
    graph.add_edge("final_answer", END)
    return graph.compile(checkpointer=InMemorySaver())


builder = build_graph()


app = BedrockAgentCoreApp()


@app.entrypoint
async def agent_handler(payload, context):
    prompt = payload.get("prompt")
    thread_id = getattr(context, "session_id", None) or payload.get("thread_id", "default_user")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Provide a nonempty prompt.")
    if not thread_id:
        raise ValueError("A runtime session ID or local thread_id is required for follow-up replies.")
    state = await builder.ainvoke(
        {"messages": [HumanMessage(content=prompt)]},
        config={"configurable": {"thread_id": thread_id}},
    )
    return {
        "answer": state["messages"][-1].content,
        "needs_input": state["needs_input"],
        "thread_id": thread_id,
        "hotels": [] if state["needs_input"] or state.get("search_error") else state.get("ranked_hotels", []),
        "search_details": state.get("search_details", {}),
        "search_error": None if state["needs_input"] else state.get("search_error"),
    }
    
    
if __name__ == "__main__":
    app.run()