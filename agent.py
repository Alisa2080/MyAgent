import dotenv

from agent_core.builders import build_agent

dotenv.load_dotenv()

agent = build_agent()
