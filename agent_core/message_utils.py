from typing import Any


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                if part:
                    text_parts.append(part)
                continue
            if isinstance(part, dict):
                text = part.get("text", "") or part.get("content", "")
                if text:
                    text_parts.append(text)
            else:
                text = getattr(part, "text", "")
                if text:
                    text_parts.append(text)
        return "\n".join(text_parts)
    return str(content) if content is not None else ""


def extract_user_request(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content", "")
        else:
            role = getattr(message, "role", None) or getattr(message, "type", None)
            content = getattr(message, "content", "")
        if role in {"user", "human"}:
            return _extract_text_content(content)
    return ""


def extract_text_from_agent_response(response: dict[str, Any]) -> str:
    messages = response.get("messages", [])
    if not messages:
        return ""
    last_message = messages[-1]
    content = getattr(last_message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    text_parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text", "") or item.get("content", "")
                if text and item.get("type") in {None, "text"}:
                    text_parts.append(text)
                continue
            item_type = getattr(item, "type", None)
            item_text = getattr(item, "text", "")
            if item_type == "text" and item_text:
                text_parts.append(item_text)
        return "\n".join(part.strip() for part in text_parts if part and part.strip())
    return ""
