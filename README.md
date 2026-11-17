# Set a Vantage reason code via the Seeq API

[set_reason_code.py](set_reason_code.py) creates a reason code hierarchy (for example "Problem Cause"), assigns it
to a Vantage room, and sets a reason code from it on one or more events. It follows the same steps as the Vantage UI.

## What it does

1. **Creates the hierarchy** from `HIERARCHY_LEVELS` / `HIERARCHY_TREE` if none with `HIERARCHY_NAME` exists.
   If one exists, the script reuses it. With `UPDATE_EXISTING = True`, it replaces the existing contents instead.
2. **Assigns it to the room**, as the room default or to specific conditions. You can only pick a hierarchy's values
   for an event when the hierarchy is assigned to that event's condition or is the room default. Creating it is not
   enough.
3. **Resolves the reason code** (`LABEL_PATH`, e.g. `["Mechanical", "Bearing Failure"]`) to one label ID per level.
4. **Finds events** in the room's evidence table that overlap `EVENT_WINDOW`, newest first.
5. **Writes one context label per level** on each event, up to `MAX_EVENTS`. The UI does the same when a user picks
   a reason code.

## Setup (local)

You need Python 3.11 and a Seeq server with reason code management enabled
(`Features/Vantage/ReasonCodesManagement/Enabled = true`).

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # then fill it in
```

`.env` holds the connection details. It is git-ignored, so never commit it:

| Variable          | Value                                                                                     |
|-------------------|-------------------------------------------------------------------------------------------|
| `SEEQ_URL`        | Server URL without a trailing slash, e.g. `https://mycompany.seeq.site`                   |
| `SEEQ_ACCESS_KEY` | Access key ID. Create one in Seeq under the user menu > Access Keys.                      |
| `SEEQ_PASSWORD`   | The access key's secret. Seeq shows it only once, when you create the key.               |
| `VANTAGE_ROOM_ID` | Optional. The room UUID from its URL. Overrides `VANTAGE_ROOM_ID` in the script.          |

Environment variables you have already set take priority over `.env`. In Seeq Data Lab you need neither `.env` nor
`python-dotenv`, because you are already logged in.

**SDK version:** the `seeq` package's major.minor version must match your server's version (Help > About in Seeq).
`requirements.txt` pins `seeq~=66.0`. For a different server version, change that line and reinstall.

## Running

```powershell
.venv\Scripts\python set_reason_code.py           # dry run: shows what would happen, changes nothing
.venv\Scripts\python set_reason_code.py --apply   # creates/assigns the hierarchy and writes the reason code
```

Always start with a dry run. It lists the existing hierarchies and their assignments, the events it found, and which
event(s) it would label.

## Configuration

Set these at the top of the script:

| Setting           | Meaning                                                                                              |
|-------------------|------------------------------------------------------------------------------------------------------|
| `VANTAGE_ROOM_ID` | The room to work in. The `.env` value wins if set.                                                    |
| `HIERARCHY_NAME`  | The hierarchy to create, or to reuse if it exists (name match ignores case).                          |
| `HIERARCHY_LEVELS`| One name per level, top first. They become the column headers in the room's table.                    |
| `HIERARCHY_TREE`  | The choices as nested dicts, one level per nesting. A leaf is `{}`.                                   |
| `UPDATE_EXISTING` | `True` replaces an existing hierarchy's contents with `HIERARCHY_TREE`. Labels not in the tree are removed. |
| `ASSIGN_TO`       | `"room-default"`, a list of condition IDs (merged with existing ones), or `None` to leave assignments as they are. |
| `LABEL_PATH`      | The reason code to set, one label per level, root first.                                              |
| `EVENT_WINDOW`    | Only events that overlap this time range are considered.                                              |
| `CONDITION_ID`    | Optional. Only consider events of this condition.                                                     |
| `MAX_EVENTS`      | Safety cap on how many events get labeled per run, newest first.                                      |

## Things to know

- **Permissions:** assigning or updating a hierarchy needs Manage permission on the room, or Analytics Administration.
- **Experimental API:** the reason code endpoints are marked experimental. The SDK prints `DeprecationWarning`s for
  them, and they may change between releases.
- **Room default is exclusive:** making a hierarchy the room default replaces any other room-default hierarchy.
  Assigning to conditions replaces this hierarchy's condition list for the room. The script merges in the existing
  conditions first.
- **Re-running writes again:** a second `--apply` reuses the hierarchy, but writes the labels on the selected events
  again. It doesn't check whether an event already has a reason code. Narrow `EVENT_WINDOW`, `CONDITION_ID` or
  `MAX_EVENTS` first.
- **Evidence table lookup:** newer servers (seen on R66.233) give the room's evidence table ID in the item's
  `evidence_table_definition_id` field. Older servers keep it in a `tableIds` property. The script tries both.
- **Event IDs:** in the evidence table, `item id` is the condition ID and `datum id` is the capsule ID.

## Checking the result

Labels written to an event appear in the evidence table's `context_labels.*` columns, one row per label:

```python
t = sdk.TablesApi(spy.session.client).query_table_data(
    table_id=evidence_table_id(VANTAGE_ROOM_ID),
    body=sdk.TableDataQueryInputV1(
        columns_to_include=["datum id", "context_labels.label_name", "context_labels.category_name"], limit=1000),
)
```

Filter the rows on `datum id` in Python. A server-side `IN` filter on `datum id` returned no rows when we tested it.
