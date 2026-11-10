"""Memory Explorer: browse/edit Working, Long-term and Episodic memory through the API."""

from __future__ import annotations

from typing import Any

import streamlit as st

from nanomemory.dashboard.client import ApiClient
from nanomemory.dashboard.components.ui import (
    admin_token_input,
    consume_admin_token,
    safe_call,
    show_error,
)


def _working_tab(client: ApiClient) -> None:
    st.subheader("Working Memory")
    snapshot = safe_call("explorer_working", client.get_working)
    if not snapshot:
        return
    document = snapshot["document"]
    with st.form("working_form"):
        goal = st.text_input("Current goal", value=document["current_goal"])
        status = st.selectbox(
            "Status",
            ["idle", "in_progress", "blocked", "completed", "cancelled"],
            index=["idle", "in_progress", "blocked", "completed", "cancelled"].index(
                document["status"]
            ),
        )
        next_action = st.text_input("Next action", value=document["next_action"])
        in_progress = st.text_area(
            "In progress (one per line)", value="\n".join(document.get("in_progress", []))
        )
        blockers = st.text_area(
            "Blockers (one per line)", value="\n".join(document.get("blockers", []))
        )
        submitted = st.form_submit_button("Save update")
    if submitted:
        payload = {
            "expected_version": snapshot["memory_version"],
            "current_goal": goal,
            "status": status,
            "next_action": next_action,
            "in_progress": [line for line in in_progress.splitlines() if line.strip()],
            "blockers": [line for line in blockers.splitlines() if line.strip()],
        }
        result = safe_call("working_update", lambda: client.put_working(payload))
        if result:
            st.success(f"Saved as memory_version {result['memory_version']}")
            st.rerun()
    st.caption(f"content hash: {snapshot['content_sha256'][:16]}…")
    with st.expander("Raw markdown"):
        st.code(snapshot["markdown"], language="markdown")
    compress_mode = st.selectbox("Compression mode", ["deterministic", "llm_assisted"])
    if st.button("Preview compression"):
        preview = safe_call(
            "compress_preview",
            lambda: client.compress(mode=compress_mode, apply=False),
        )
        if preview:
            st.json(preview)


def _long_term_tab(client: ApiClient) -> None:
    st.subheader("Long-term Memory")
    search = st.text_input("Filter by category or tag", value="")
    params: dict[str, Any] = {"limit": 100, "include_archived": True}
    if search.strip():
        params["category"] = search.strip()
    listing = safe_call("long_term_list", lambda: client.list_long_term(**params))
    if listing:
        st.caption(
            f"{listing['count']} records · capacity "
            f"{listing['capacity'].get('capacity_used')}/{listing['capacity'].get('capacity_limit')}"
        )
        for record in listing["records"]:
            with st.expander(f"{record['category']} · {record['content'][:60]}…"):
                st.write(record["content"])
                st.caption(
                    f"id {record['id']} · v{record['version']} · status {record['status']} · "
                    f"confidence {record['confidence']} · importance {record['importance']}"
                )
                st.caption(f"source: {record['source']} ({record['source_type']})")
                if record["status"] != "archived":
                    if st.button("Archive", key=f"archive_{record['id']}"):
                        result = safe_call(
                            "archive",
                            lambda rid=record["id"], ver=record["version"]: client.delete_long_term(
                                rid, if_version=ver
                            ),
                        )
                        if result:
                            st.success("Archived")
                            st.rerun()
    with st.form("long_term_form"):
        content = st.text_area("New memory content")
        category = st.text_input("Category", value="general")
        tags = st.text_input("Tags (comma separated)")
        importance = st.slider("Importance", 1, 5, 3)
        submitted = st.form_submit_button("Add memory")
    if submitted and content.strip():
        payload = {
            "content": content,
            "category": category or "general",
            "tags": [tag.strip() for tag in tags.split(",") if tag.strip()],
            "importance": importance,
            "source": "dashboard",
            "source_type": "user",
        }
        created = safe_call("long_term_create", lambda: client.create_long_term(payload))
        if created:
            st.success(f"Added {created['id']}")
            st.rerun()


def _episodes_tab(client: ApiClient) -> None:
    st.subheader("Episodic Memory")
    include_corrected = st.checkbox("Include corrected events (raw audit view)", value=False)
    listing = safe_call(
        "episodes",
        lambda: client.list_episodes(limit=200, include_corrected=include_corrected),
    )
    if listing:
        st.caption(
            f"{listing['count']} events · corrupt lines {listing['corrupt_lines']} · "
            f"quarantined {listing['quarantined']}"
        )
        for item in listing["events"][:50]:
            episode = item["episode"]
            badge = ""
            if item.get("corrected"):
                badge = " (corrected)"
            if item.get("correction_status") == "unresolved":
                badge = f" (correction unresolved: target {item.get('target_status')})"
            st.markdown(
                f"**{episode['timestamp']} · {episode['event_type']}**{badge}  \n"
                f"{episode['summary']}"
            )
    if st.button("Check quarantine"):
        quarantine = safe_call("quarantine", client.quarantine)
        if quarantine:
            st.session_state["quarantine_records"] = quarantine["records"]

    records = st.session_state.get("quarantine_records") or []
    if records:
        st.warning(
            "Quarantined lines block new appends until acknowledged. Acknowledgement is not a "
            "repair: the original events.jsonl is never modified."
        )
        st.json(records[:10])
        token = admin_token_input("quarantine_ack")
        reason = st.text_input("Reason for acknowledgement", key="ack_reason")
        confirmed = st.checkbox("I understand this only permits future appends", key="ack_confirm")
        if st.button("Acknowledge selected records", disabled=not (token and confirmed and reason)):
            ids = [record["quarantine_id"] for record in records]
            try:
                result = client.acknowledge_quarantine(ids, reason, admin_token=token or "")
            except Exception as error:  # noqa: BLE001 - rendered for the operator
                show_error(error)  # type: ignore[arg-type]
            else:
                st.success(f"Acknowledged {result['count']} record(s)")
                consume_admin_token("quarantine_ack")
                st.session_state.pop("quarantine_records", None)
                st.rerun()


def render(client: ApiClient) -> None:
    st.header("Memory Explorer")
    working, long_term, episodes = st.tabs(["Working", "Long-term", "Episodes"])
    with working:
        _working_tab(client)
    with long_term:
        _long_term_tab(client)
    with episodes:
        _episodes_tab(client)
