import json
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from tools import search_flights, book_flight, get_flight_by_id

SYSTEM_PROMPT = """You are a helpful Flight Booking Assistant.
Rules:
1. Use `search_flights` whenever a user asks for flights, fares, or availability. Use 3-letter airport codes (DEL for Delhi, BOM for Mumbai, BLR for Bengaluru) and YYYY-MM-DD dates.
2. If required search details (origin, destination, or date) are missing, ask a brief clarifying question instead of guessing.
3. NEVER call `book_flight` unless the user has explicitly typed their real full name and age in the conversation. NEVER use placeholder values like "John Doe" or age 30. If the user only says "yes" without giving their name and age, ask: "Please provide the passenger's full name and age to proceed."
4. State only facts returned by tools. If a booking is cancelled by the user, acknowledge it without claiming a ticket was booked.
"""

MAX_STEPS = 6


def prompt_yes_no_confirmation(tool_args: dict) -> bool:
    """Enforce an explicit yes/no confirmation in the terminal before a write tool runs."""
    flight_id = str(tool_args.get("flight_id", "")).strip()
    passenger_name = str(tool_args.get("passenger_name", "")).strip()
    passenger_age = tool_args.get("passenger_age", "")

    flight = get_flight_by_id(flight_id)
    print("\n" + "=" * 48)
    print("CONFIRMATION REQUIRED BEFORE BOOKING")
    print("=" * 48)
    if flight:
        print(f"Flight     : {flight['flight_id']} ({flight['airline']})")
        print(f"Route      : {flight['origin']} -> {flight['destination']} on {flight['date']}")
        print(f"Departure  : {flight['departure']} | Arrival: {flight['arrival']}")
        print(f"Fare       : Rs. {flight['fare_inr']}")
    else:
        print(f"Flight ID  : {flight_id}")
    print(f"Passenger  : {passenger_name} (Age: {passenger_age})")
    print("=" * 48)

    while True:
        choice = input("Proceed with booking? (yes/no): ").strip().lower()
        if choice in {"y", "yes"}:
            return True
        if choice in {"n", "no"}:
            return False
        print("Please type 'yes' or 'no'.")


def main():
    llm = ChatOllama(model="qwen2.5:3b", temperature=0)
    tools_by_name = {
        "search_flights": search_flights,
        "book_flight": book_flight,
    }
    llm_with_tools = llm.bind_tools(list(tools_by_name.values()))

    messages = [SystemMessage(content=SYSTEM_PROMPT)]
    print("Flight Booking Agent ready (type 'exit' to quit).\n")

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break

        messages.append(HumanMessage(content=user_input))

        for _ in range(MAX_STEPS):
            response = llm_with_tools.invoke(messages)
            messages.append(response)

            if not response.tool_calls:
                print(f"\nAgent: {response.content}\n")
                break

            for call in response.tool_calls:
                tool_name = call["name"]
                tool_args = call["args"]
                print(f"  [Tool Call] {tool_name}({json.dumps(tool_args)})")

                tool_fn = tools_by_name.get(tool_name)
                if not tool_fn:
                    tool_result = json.dumps({"status": "error", "message": f"Unknown tool {tool_name}"})
                elif tool_name == "book_flight":
                    approved = prompt_yes_no_confirmation(tool_args)
                    if approved:
                        tool_result = tool_fn.invoke(tool_args)
                    else:
                        tool_result = json.dumps({
                            "status": "cancelled",
                            "message": "Booking cancelled by user at confirmation prompt. No changes made."
                        })
                else:
                    tool_result = tool_fn.invoke(tool_args)

                messages.append(
                    ToolMessage(
                        content=str(tool_result),
                        tool_call_id=call["id"],
                    )
                )


if __name__ == "__main__":
    main()