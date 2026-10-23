import math
import uuid
from datetime import date

import streamlit as st

from src.agent_client import AgentInvocationError, deployment_settings, invoke_agent, safe_web_url


st.set_page_config(page_title="Stayfinder", page_icon=":material/travel_explore:", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap');
:root { --stay-green: #166450; --stay-ink: #202d29; }
html, body, [class*="css"], .stApp, button, input, textarea { font-family: 'DM Sans', sans-serif; letter-spacing: 0; }
h1, h2, h3 { font-family: 'Manrope', sans-serif !important; color: var(--stay-ink); letter-spacing: 0 !important; }
.stApp { background: linear-gradient(180deg, #f2f7f4 0, #ffffff 280px); color: var(--stay-ink); }
[data-testid="stSidebar"] { background: #f0f3f1; border-right: 1px solid #dde5df; }
.block-container { max-width: 1440px; padding-top: 2.5rem; padding-bottom: 2rem; }
[data-testid="stVerticalBlockBorderWrapper"] > div { border-radius: 8px !important; }
[data-testid="stChatMessage"] { background: #f2f5f3; border-radius: 8px; }
[data-testid="stChatInput"] { border-radius: 8px; }
[data-testid="stMetricValue"] { font-size: 1.3rem; font-family: 'Manrope', sans-serif; }
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] a { overflow-wrap: anywhere; }
.trip-label { color: #687a70; font-size: .78rem; text-transform: uppercase; letter-spacing: 0; margin: 0; }
.empty-state { padding: 5rem 1rem; text-align: center; color: #697c71; border-top: 1px solid #dce5df; }
.empty-state h3 { font-size: 1.25rem; margin-bottom: .5rem; }
@media (max-width: 640px) { .block-container { padding-top: 1.5rem; } .empty-state { padding: 2rem .5rem; } }
@media (prefers-reduced-motion: no-preference) { .block-container { animation: appear .3s ease-out; } @keyframes appear { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } } }
</style>
""", unsafe_allow_html=True)


def reset_trip():
    st.session_state.trip_id = "trip-" + str(uuid.uuid4())
    st.session_state.chat = [{"role": "assistant", "content": "Where would you like to stay?"}]
    st.session_state.hotels = []
    st.session_state.details = {}
    st.session_state.search_error = None
    st.session_state.searched = False


def numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def display_date(value: str | None) -> str:
    if not value:
        return "Dates open"
    try:
        return date.fromisoformat(value).strftime("%d %b %Y")
    except ValueError:
        return value


if "trip_id" not in st.session_state:
    reset_trip()

settings = deployment_settings()
with st.sidebar:
    st.image("https://images.unsplash.com/photo-1512470876302-972faa2aa9a4?auto=format&fit=crop&w=600&q=80")
    st.subheader("Your trip")
    if st.button("New trip", icon=":material/add:", use_container_width=True):
        reset_trip()
    connection = st.radio("Agent connection", ["AWS runtime", "Local dev"])
    if connection == "AWS runtime":
        runtime_arn = st.text_input("Runtime ARN", value=settings["runtime_arn"])
        region = st.text_input("AWS region", value=settings["region"])
        local_url = ""
    else:
        local_url = st.text_input("Agent URL", value="http://127.0.0.1:8080")
        runtime_arn, region = "", settings["region"]
    signature = (connection, runtime_arn, region, local_url)
    if st.session_state.get("connection_signature", signature) != signature:
        reset_trip()
    st.session_state.connection_signature = signature
    st.divider()
    details = st.session_state.details
    st.markdown(f"**{details.get('destination') or 'Destination open'}**")
    st.caption(display_date(details.get("checkin_date")) + " - " + display_date(details.get("checkout_date")))
    if details.get("number_of_adults"):
        st.caption(f"{details['number_of_adults']} adults / {details.get('number_of_rooms', '?')} rooms")
    if numeric(details.get("budget")):
        st.metric("Total-stay budget", f"{details.get('currency', '')} {details['budget']:,.0f}")
    st.caption("Read-only search. No bookings or payments.")

st.markdown('<p class="trip-label">STAYFINDER</p>', unsafe_allow_html=True)
st.title("Find your next stay")
chat_column, results_column = st.columns([1, 1.35], gap="large")

with chat_column:
    st.subheader("Conversation")
    conversation = st.container(height=530, border=False)
    with conversation:
        for message in st.session_state.chat:
            avatar = ":material/person:" if message["role"] == "user" else ":material/travel_explore:"
            with st.chat_message(message["role"], avatar=avatar):
                st.markdown(message["content"])
    prompt = st.chat_input("Tell me about your trip", disabled=not (runtime_arn if connection == "AWS runtime" else local_url))
    if prompt:
        st.session_state.chat.append({"role": "user", "content": prompt})
        with conversation:
            with st.chat_message("user", avatar=":material/person:"):
                st.markdown(prompt)
            with st.spinner("Checking your trip..."):
                try:
                    reply = invoke_agent(
                        prompt, st.session_state.trip_id,
                        connection=connection, runtime_arn=runtime_arn, region=region, local_url=local_url,
                    )
                    hotels = reply.get("hotels", [])
                    st.session_state.hotels = [hotel for hotel in hotels if isinstance(hotel, dict)] if isinstance(hotels, list) else []
                    st.session_state.details = reply.get("search_details") or st.session_state.details
                    st.session_state.search_error = reply.get("search_error")
                    st.session_state.searched = not reply.get("needs_input", True)
                    answer = reply["answer"]
                    if st.session_state.hotels:
                        destination = st.session_state.details.get("destination", "your destination")
                        answer = f"Found {len(st.session_state.hotels)} stays in {destination} within your total-stay budget."
                except AgentInvocationError as error:
                    answer = str(error)
                    st.session_state.hotels = []
                    st.session_state.search_error = answer
                except Exception:
                    answer = "The agent connection could not be completed. Check your configuration and server logs."
                    st.session_state.hotels = []
                    st.session_state.search_error = answer
                st.session_state.chat.append({"role": "assistant", "content": answer})
        st.rerun()

with results_column:
    title, ordering = st.columns([1, 1])
    with title:
        st.subheader("Your shortlist")
    with ordering:
        sort_order = st.selectbox("Sort stays", ["Recommended", "Lowest total price", "Guest rating"], label_visibility="collapsed")
    hotels = st.session_state.hotels
    if st.session_state.search_error:
        st.error(st.session_state.search_error, icon=":material/error:")
    elif hotels:
        if sort_order == "Lowest total price":
            hotels = sorted(hotels, key=lambda hotel: hotel.get("price") if numeric(hotel.get("price")) else float("inf"))
        elif sort_order == "Guest rating":
            hotels = sorted(hotels, key=lambda hotel: -(hotel.get("rating") if numeric(hotel.get("rating")) else -1))
        st.caption(f"{len(hotels)} stays returned / Prices for the entire stay")
        for index, hotel in enumerate(hotels):
            with st.container(border=True):
                name_column, price_column = st.columns([1.7, 1])
                with name_column:
                    st.markdown(f"**{index + 1}. {hotel.get('name') or 'Unnamed accommodation'}**")
                    score = hotel.get("rating")
                    if numeric(score):
                        st.caption(f"Guest score {score:g} / 10")
                    else:
                        st.caption("Guest score unavailable")
                with price_column:
                    price = hotel.get("price")
                    currency = hotel.get("currency") or st.session_state.details.get("currency", "")
                    st.metric("Total stay", f"{currency} {price:,.2f}" if numeric(price) else "Unavailable", label_visibility="collapsed")
                    url = safe_web_url(hotel.get("url"))
                    if url:
                        st.link_button("View property", url, icon=":material/open_in_new:", use_container_width=True)
    elif st.session_state.searched:
        st.info("No stays matched this search.", icon=":material/search_off:")
    else:
        st.markdown('<div class="empty-state"><h3>A stay worth looking forward to.</h3><p>Your shortlist is still open.</p></div>', unsafe_allow_html=True)