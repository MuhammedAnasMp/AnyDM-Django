import os
from google import genai

# never hardcode in real apps
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ---------------------------
# 🛒 Ecommerce Context (static for now)
# ---------------------------
ECOMMERCE_CONTEXT = """
You are an ecommerce assistant.

Store Name: TechKart
We sell:
- Smartphones (iPhone, Samsung, Xiaomi)
- Laptops (Dell, HP, MacBook)
- Accessories (headphones, chargers, keyboards)

Rules:
- Reply in short, helpful sentences
- Be friendly and supportive
- Help users find products
- Suggest based on user interest
"""

# ---------------------------
# 📊 Simple user activity tracker
# (future: replace with database)
# ---------------------------
user_activity = {
    "messages": [],
    "search_history": [],
    "last_query": None
}


def build_prompt(user_input):
    user_activity["messages"].append(user_input)
    user_activity["last_query"] = user_input

    # simple intent tracking
    if "phone" in user_input.lower():
        user_activity["search_history"].append("phones")
    if "laptop" in user_input.lower():
        user_activity["search_history"].append("laptops")

    prompt = f"""
{ECOMMERCE_CONTEXT}

User activity so far:
- Messages: {user_activity["messages"][-5:]}
- Interests: {user_activity["search_history"]}

User: {user_input}

Respond in:
- short sentences
- supportive tone
- ecommerce focused
"""
    return prompt


# ---------------------------
# 💬 Chat Loop
# ---------------------------
while True:
    user_input = input("You: ")

    if user_input.lower() in ["exit", "quit"]:
        print("AI: Bye 👋 come back anytime!")
        break

    prompt = build_prompt(user_input)

    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt
    )

    print("AI:", response.text)
