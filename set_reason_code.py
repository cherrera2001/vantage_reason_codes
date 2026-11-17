"""
Example: create a Vantage reason code hierarchy (e.g. "Problem Cause"), assign it to a room, and set a reason code
from it on an event via the Seeq API.

What this does, in the same order as the Vantage UI:

  1. Creates the reason code hierarchy defined in HIERARCHY_LEVELS / HIERARCHY_TREE if no hierarchy with that name
     exists yet (or replaces its contents when UPDATE_EXISTING = True).
  2. Assigns the hierarchy to the room. A hierarchy's values only show up for an event when the hierarchy is assigned
     to that event's condition (or is the room default). Creating a hierarchy is not enough on its own -- this is the
     usual reason "I created a hierarchy but can't select its values".
  3. Resolves a label path such as ["Mechanical", "Bearing Failure"] to label IDs using the hierarchy tree.
  4. Finds events in the room's evidence table (item id = condition ID, datum id = capsule ID).
  5. Writes one context label per level (root -> leaf) on the event, which is exactly what the UI does
     when a user picks a reason code.

Requirements:
  * Seeq server with the reason code hierarchy API (Features/Vantage/ReasonCodesManagement/Enabled = true).
    These endpoints are marked experimental and may change between releases.
  * Assigning (and updating) a hierarchy needs Manage permission on the Vantage room (or Analytics Administration).
  * Run inside Seeq Data Lab (already logged in), or locally with SEEQ_URL / SEEQ_ACCESS_KEY / SEEQ_PASSWORD set in a
    .env file next to this script (copy .env.example) or in the environment. See README.md for local setup.

Usage:
  python set_reason_code.py            # dry run: prints what it would create/assign/write, changes nothing
  python set_reason_code.py --apply    # creates/assigns the hierarchy and writes the reason code

Re-running with --apply reuses the hierarchy, but writes the labels again on the selected events. It does not check
whether an event already has a reason code.
"""
import argparse
import json
import os
from typing import Dict, List, Optional

import pandas as pd
from seeq import sdk, spy

# ---------------------------------------------------------------------------------------------------------------------
# Configuration -- edit these
# ---------------------------------------------------------------------------------------------------------------------

# Vantage room (workbook) ID: the UUID in the room's URL, e.g. https://server/workbook/<this-id>/...
VANTAGE_ROOM_ID = "00000000-0000-0000-0000-000000000000"

# The reason code hierarchy to create (or reuse, if one with this name already exists)
HIERARCHY_NAME = "Problem Cause"

# One name per level, top level first. These become the column headers in the room's table.
HIERARCHY_LEVELS = ["Cause Category", "Cause"]

# The choices. Nest one dict per level; a leaf is an empty dict. Every branch may stop at any depth.
HIERARCHY_TREE: Dict[str, dict] = {
    "Mechanical": {
        "Bearing Failure": {},
        "Seal Leak": {},
        "Misalignment": {},
    },
    "Electrical": {
        "Motor Trip": {},
        "Power Loss": {},
    },
    "Process": {
        "Feed Upset": {},
        "Operator Error": {},
    },
}

# False: reuse an existing hierarchy of the same name as-is.
# True:  replace its levels/labels with HIERARCHY_TREE (labels missing from the tree are removed from the hierarchy).
UPDATE_EXISTING = False

# Assignment of the hierarchy to the room. Set to:
#   "room-default"     -> hierarchy covers every condition in the room that has no explicit assignment
#   ["<cond-id>", ...] -> explicit list of conditions (merged with conditions already assigned to this hierarchy)
#   None               -> leave assignments untouched
ASSIGN_TO = "room-default"

# The reason code to set: one label per level, root first (case-insensitive). Must exist in the hierarchy.
LABEL_PATH = ["Mechanical", "Bearing Failure"]

# Which events to set the reason code on: events overlapping this window...
EVENT_WINDOW = ("2026-11-01T00:00:00Z", "2026-11-16T00:00:00Z")
# ...optionally restricted to one condition (None = any condition in the room)
CONDITION_ID: Optional[str] = None
# Safety cap on how many events get written in one run (most recent first)
MAX_EVENTS = 1

# Only needed when running outside Seeq Data Lab. Read from the environment, which is populated from a .env file next to
# this script when python-dotenv is installed (see .env.example). Real environment variables take precedence.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:  # e.g. in Seeq Data Lab, where no .env is needed
    pass

VANTAGE_ROOM_ID = os.environ.get("VANTAGE_ROOM_ID") or VANTAGE_ROOM_ID
SEEQ_URL = os.environ.get("SEEQ_URL", "")
SEEQ_ACCESS_KEY = os.environ.get("SEEQ_ACCESS_KEY", "")
SEEQ_PASSWORD = os.environ.get("SEEQ_PASSWORD", "")


# ---------------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------------


def login() -> None:
    if spy.session.client is not None:
        return
    if not SEEQ_URL:
        raise RuntimeError("Not logged in. Run in Seeq Data Lab, or set SEEQ_URL / SEEQ_ACCESS_KEY / SEEQ_PASSWORD.")
    spy.login(url=SEEQ_URL, access_key=SEEQ_ACCESS_KEY, password=SEEQ_PASSWORD)


def same(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def build_hierarchy_input(name: str, levels: List[str], tree: Dict[str, dict]) -> sdk.ReasonCodeHierarchyInputV1:
    """Convert the nested dict into the API's node tree. Each node names its level; the prompt mirrors the level."""

    def nodes(branch: Dict[str, dict], depth: int) -> List[sdk.ReasonCodeNodeInputV1]:
        if branch and depth >= len(levels):
            raise ValueError(f"HIERARCHY_TREE is deeper than HIERARCHY_LEVELS ({len(levels)} levels).")
        return [
            sdk.ReasonCodeNodeInputV1(
                name=label,
                level_name=levels[depth],
                level_prompt=levels[depth],
                children=nodes(children, depth + 1) if children else None,
            )
            for label, children in branch.items()
        ]

    return sdk.ReasonCodeHierarchyInputV1(name=name, nodes=nodes(tree, 0))


def ensure_hierarchy(context_api: sdk.ContextApi, room_id: str, apply: bool):
    """Create (or reuse / update) the hierarchy. Returns (hierarchy id or None on a dry run, its room assignments)."""
    listing = context_api.list_reason_code_hierarchies(workbook_id=room_id)
    hierarchies = listing.hierarchies or []
    print("Existing reason code hierarchies:")
    for h in hierarchies:
        print(f"  {h.name!r:30} id={h.id} archived={h.archived}")

    existing = next((h for h in hierarchies if same(h.name, HIERARCHY_NAME) and not h.archived), None)
    body = build_hierarchy_input(HIERARCHY_NAME, HIERARCHY_LEVELS, HIERARCHY_TREE)

    if existing is None:
        if not apply:
            print(f"\n[dry run] would create hierarchy {HIERARCHY_NAME!r} with levels {HIERARCHY_LEVELS}")
            return None, []
        created = context_api.create_reason_code_hierarchy(body=body)
        print(f"\nCreated hierarchy {created.name!r} id={created.id}")
        return created.id, []

    if UPDATE_EXISTING:
        if apply:
            context_api.update_reason_code_hierarchy(category_id=existing.id, workbook_id=room_id, body=body)
            print(f"\nUpdated hierarchy {existing.name!r} id={existing.id} from HIERARCHY_TREE")
        else:
            print(f"\n[dry run] would replace the contents of {existing.name!r} with HIERARCHY_TREE")
    else:
        print(f"\nReusing existing hierarchy {existing.name!r} id={existing.id} (UPDATE_EXISTING = False)")

    assignments = [
        a for a in listing.condition_assignments or [] if same(a.hierarchy_category_id, existing.id)
    ]
    print(f"Current assignments of {existing.name!r} in this room:")
    if not assignments:
        print("  NONE -- the hierarchy is not assigned to any condition, so its values cannot be selected on events.")
    for a in assignments:
        print("  room default" if a.condition_id is None else f"  condition {a.condition_id}")
    return existing.id, assignments


def assign_hierarchy(context_api: sdk.ContextApi, room_id: str, hierarchy_id: str, existing, assign_to) -> None:
    """Assign the hierarchy. NOTE: the endpoint fully replaces this hierarchy's condition list for the room."""
    if assign_to == "room-default":
        # Omitting condition_ids makes it the room default (replaces any other room-default hierarchy)
        body = sdk.ReasonCodeAssignmentInputV1(workbook_id=room_id)
        print(f"Assigning hierarchy {hierarchy_id} as the room default")
    else:
        already = [a.condition_id for a in existing if a.condition_id]
        condition_ids = sorted({c.upper() for c in already + list(assign_to)})
        body = sdk.ReasonCodeAssignmentInputV1(workbook_id=room_id, condition_ids=condition_ids)
        print(f"Assigning hierarchy {hierarchy_id} to conditions {condition_ids}")
    context_api.assign_reason_code_hierarchy(category_id=hierarchy_id, body=body)


def resolve_label_ids(context_api: sdk.ContextApi, hierarchy_id: str, label_path: List[str]) -> List[str]:
    """Return the label ID for each entry of label_path, following parent links from the root.

    The tree endpoint returns levels ordered by name, not depth, so walk parent_id pointers instead of level order.
    """
    tree = context_api.get_reason_code_hierarchy_tree(category_id=hierarchy_id)
    labels = [(level.name, label) for level in tree.levels or [] for label in level.labels or []]

    label_ids: List[str] = []
    parent_id: Optional[str] = None
    for depth, wanted in enumerate(label_path):
        choices = [(level, label) for level, label in labels if same(label.parent_id, parent_id)]
        match = next((label for _, label in choices if same(label.name, wanted)), None)
        if match is None:
            valid = sorted(label.name for _, label in choices)
            raise ValueError(f"{wanted!r} is not a valid choice at depth {depth + 1}. Valid choices: {valid}")
        parent_id = match.id
        label_ids.append(match.id)
    return label_ids


def evidence_table_id(room_id: str) -> str:
    """The room's evidence (materialized) table holds one row per event with its condition and capsule IDs."""
    room = sdk.ItemsApi(spy.session.client).get_item_and_all_properties(id=room_id)
    # Newer servers expose it directly on the item; older ones keep it in the "tableIds" property.
    if getattr(room, "evidence_table_definition_id", None):
        return room.evidence_table_definition_id
    for prop in room.properties or []:
        if prop.name == "tableIds":
            table_ids = json.loads(prop.value)
            if isinstance(table_ids, dict) and table_ids.get("evidence"):
                return table_ids["evidence"]
    raise ValueError(f"Could not find the evidence table for Vantage room {room_id}. Is this a Vantage room ID?")


def find_events(room_id: str, window, condition_id: Optional[str]) -> pd.DataFrame:
    """Return events overlapping the window, with 'item id' (condition), 'datum id' (capsule), Start and End."""
    start_ns = int(pd.Timestamp(window[0]).value)
    end_ns = int(pd.Timestamp(window[1]).value)

    def value_filter(column, op, value):
        return sdk.Filter(value_filter=sdk.ValueFilter(column_name=column, filter_type=op, value=value))

    def both(f1, f2):
        return sdk.Filter(composite_filter=sdk.CompositeFilter(filter1=f1, filter2=f2, operation="AND"))

    def either(f1, f2):
        return sdk.Filter(composite_filter=sdk.CompositeFilter(filter1=f1, filter2=f2, operation="OR"))

    end_open = sdk.Filter(simple_filter=sdk.SimpleFilter(column_name="End", filter_type="IS_NULL"))
    overlap = both(
        value_filter("Start", "LESS_THAN", end_ns), either(value_filter("End", "GREATER_THAN", start_ns), end_open)
    )
    if condition_id:
        overlap = both(overlap, value_filter("item id", "IN", [condition_id]))

    table = sdk.TablesApi(spy.session.client).query_table_data(
        table_id=evidence_table_id(room_id),
        body=sdk.TableDataQueryInputV1(
            columns_to_include=["item id", "datum id", "Condition", "Start", "End"],
            filter=overlap,
            sorts=[sdk.Sort(column_name="Start", ascending=False)],
            limit=1000,
        ),
    )
    df = pd.DataFrame(table.rows or [], columns=[h.name for h in table.headers or []])
    for column in ("Start", "End"):
        if column in df and pd.api.types.is_numeric_dtype(df[column]):
            df[column] = pd.to_datetime(df[column], unit="ns", utc=True)
        elif column in df:
            df[column] = pd.to_datetime(df[column], utc=True)
    return df


def set_reason_code(context_api: sdk.ContextApi, event: pd.Series, label_ids: List[str]) -> None:
    """Write one context label per hierarchy level onto the event, root first (same as the Vantage UI)."""
    end = event["End"] if pd.notna(event["End"]) else pd.Timestamp.now(tz="UTC")  # in-progress event: up to now
    for label_id in label_ids:
        created = context_api.create_context_label(
            item_id=event["item id"],
            body=sdk.ContextLabelInputV1(
                datum_id=event["datum id"],
                label_id=label_id,
                start_time=event["Start"].isoformat(),
                end_time=end.isoformat(),
            ),
        )
        print(f"    created context label {created.context_id} (label {label_id})")


# ---------------------------------------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    args = parser.parse_args()

    login()
    context_api = sdk.ContextApi(spy.session.client)

    # 1. Create / reuse the hierarchy
    hierarchy_id, assignments = ensure_hierarchy(context_api, VANTAGE_ROOM_ID, args.apply)

    # 2. Assign it to the room
    if ASSIGN_TO is not None:
        if args.apply and hierarchy_id:
            assign_hierarchy(context_api, VANTAGE_ROOM_ID, hierarchy_id, assignments, ASSIGN_TO)
        else:
            print(f"[dry run] would assign {HIERARCHY_NAME!r} to {ASSIGN_TO}")

    # 3. Resolve the reason code to label IDs (needs the hierarchy to exist)
    label_ids: List[str] = []
    if hierarchy_id:
        label_ids = resolve_label_ids(context_api, hierarchy_id, LABEL_PATH)
        print(f"\nLabel path {LABEL_PATH} -> label IDs {label_ids}")
    else:
        print(f"\n[dry run] hierarchy not created yet; {LABEL_PATH} will be resolved after --apply creates it")

    # 4. Find events
    events = find_events(VANTAGE_ROOM_ID, EVENT_WINDOW, CONDITION_ID)
    print(f"\nFound {len(events)} event(s) in {EVENT_WINDOW}:")
    print(events.head(20).to_string(index=False))

    # 5. Set the reason code
    for _, event in events.head(MAX_EVENTS).iterrows():
        target = f"{event.get('Condition', event['item id'])} @ {event['Start']}"
        if args.apply:
            print(f"\nSetting {' > '.join(LABEL_PATH)} on {target}")
            set_reason_code(context_api, event, label_ids)
        else:
            print(f"\n[dry run] would set {' > '.join(LABEL_PATH)} on {target}")


if __name__ == "__main__":
    main()
