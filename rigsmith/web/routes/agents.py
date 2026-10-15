"""The chat and its agents."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...core import chat
from ..plain import PlainRoute

router = APIRouter(prefix="/api/chat", route_class=PlainRoute)


class NewChat(BaseModel):
    role: str = "assistant"
    title: str | None = None
    allow_writes: bool = False


class Message(BaseModel):
    text: str


class ChatEdit(BaseModel):
    title: str | None = None
    allow_writes: bool | None = None


class Decision(BaseModel):
    allow: bool


class Dispatch(BaseModel):
    role: str
    task: str
    allow_writes: bool = False


@router.get("")
def list_chats() -> dict:
    return {"chats": [c.to_dict(full=False) for c in chat.list_chats()]}


@router.post("")
def create(body: NewChat) -> dict:
    return chat.create(role=body.role, title=body.title, allow_writes=body.allow_writes).to_dict()


@router.get("/roles")
def roles() -> dict:
    from ...agent import roles as role_module
    from ...agent import tools

    return {
        "roles": [
            {**r.to_dict(), "tool_count": len(tools.select(r.tools, groups_wanted=r.groups))}
            for r in role_module.ROLES.values()
        ]
    }


@router.get("/providers")
def providers() -> dict:
    from ...agent import providers as provider_module

    return provider_module.status()


@router.get("/approvals")
def approvals(chat_id: str | None = None) -> dict:
    return {"approvals": chat.pending_approvals(chat_id)}


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: Decision) -> dict:
    return chat.decide(approval_id, body.allow)


@router.get("/agents")
def agent_runs() -> dict:
    return {"runs": chat.list_runs()}


@router.post("/agents")
def dispatch(body: Dispatch) -> dict:
    message = chat.dispatch(body.role, body.task, allow_writes=body.allow_writes)
    return {"message": message, "run_id": message.split("run ")[1].split(".")[0]}


@router.get("/agents/{run_id}")
def agent_run(run_id: str) -> dict:
    return chat.agent_run(run_id)


@router.get("/{chat_id}")
def get_chat(chat_id: str) -> dict:
    return chat.get(chat_id).to_dict()


@router.patch("/{chat_id}")
def edit(chat_id: str, body: ChatEdit) -> dict:
    return chat.update(chat_id, title=body.title, allow_writes=body.allow_writes).to_dict()


@router.delete("/{chat_id}")
def delete(chat_id: str) -> dict:
    chat.delete(chat_id)
    return {"deleted": chat_id}


@router.post("/{chat_id}/messages")
def send(chat_id: str, body: Message) -> dict:
    return chat.send(chat_id, body.text)


@router.post("/{chat_id}/stop")
def stop(chat_id: str) -> dict:
    return chat.stop(chat_id)


@router.post("/{chat_id}/clear")
def clear(chat_id: str) -> dict:
    return chat.clear(chat_id).to_dict()
