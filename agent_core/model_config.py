import os

import dotenv
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

dotenv.load_dotenv()

# if os.getenv("ANTHROPIC_BASE_URL"):
#     os.environ["ANTHROPIC_BASE_URL"] = os.getenv("ANTHROPIC_BASE_URL", "")
# if os.getenv("ANTHROPIC_API_KEY"):
#     os.environ["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_API_KEY", "")

# MAIN_MODEL = ChatAnthropic(model="mimo-v2.5-pro")

# SMALL_MODEL = ChatAnthropic(model="mimo-v2.5")

    
if os.getenv("OPENAI_BASE_URL"):
    os.environ["OPENAI_BASE_URL"] = os.getenv("OPENAI_BASE_URL", "")
if os.getenv("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY", "")


MAIN_MODEL = ChatOpenAI(model="gpt-5.4")

SMALL_MODEL = ChatOpenAI(model="gpt-5.4-mini")
