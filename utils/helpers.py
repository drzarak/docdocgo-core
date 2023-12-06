import os
import json
import re
from datetime import datetime

DELIMITER = "-" * 90 + "\n"
INTRO_ASCII_ART = """ ,___,   ,___,   ,___,                                                 ,___,   ,___,   ,___,
 [OvO]   [OvO]   [OvO]                                                 [OvO]   [OvO]   [OvO]
 /)__)   /)__)   /)__)               WELCOME TO DOC DOC GO             /)__)   /)__)   /)__)
--"--"----"--"----"--"--------------------------------------------------"--"----"--"----"--"--"""

MAIN_BOT_PREFIX = "DocDocGo: "

RETRY_COMMAND_ID = 0
CHAT_WITH_DOCS_COMMAND_ID = 1
DETAILS_COMMAND_ID = 2
QUOTES_COMMAND_ID = 3
WEB_COMMAND_ID = 4
ITERATIVE_RESEARCH_COMMAND_ID = 5
JUST_CHAT_COMMAND_ID = 6
SWITCH_DB_COMMAND_ID = 7

command_ids = {
    "/chat": JUST_CHAT_COMMAND_ID,
    "/docs": CHAT_WITH_DOCS_COMMAND_ID,
    "/details": DETAILS_COMMAND_ID,
    "/quotes": QUOTES_COMMAND_ID,
    "/web": WEB_COMMAND_ID,
    "/research": ITERATIVE_RESEARCH_COMMAND_ID,
    "/db": SWITCH_DB_COMMAND_ID,
}

DEFAULT_MODE = os.getenv("DEFAULT_MODE", "/docs")
DEFAULT_MODE_ID = command_ids[DEFAULT_MODE]

HINT_MESSAGE = (
    f"Hints: "
    f'Type your query (or "exit"). You can also use the following prefixes:\n'
    f"/docs: chat with the bot about your docs or anything else\n"
    f"/details: get details about the retrieved documents\n"
    f"/quotes: get quotes from the retrieved documents\n"
    f"/web: perform web searches and generate a report\n"
    f"/research: perform iterative research (no message = iterate on previous report)\n"
    f"/chat: regular chat with the bot, without retrieving docs or websites\n"
    f"/db: select new document database\n"
    f'\nExample: "/web openai news"\n'
    f"{DELIMITER}"
)


def print_no_newline(*args, **kwargs):
    """
    Print without adding a newline at the end
    """
    print(*args, **kwargs, end="", flush=True)


def is_directory_empty(directory):
    return not os.listdir(directory)


def clear_directory(directory):
    """
    Remove all files and subdirectories in the given directory.
    """
    import shutil

    errors = []
    for item in os.listdir(directory):
        item_path = os.path.join(directory, item)
        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.unlink(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
        except Exception as e:
            errors.append(e)
    if errors:
        err_str = "\n".join([str(e) for e in errors])
        raise Exception("Could not delete all items:\n" + err_str)


def lin_interpolate(x, x_min, x_max, y_min, y_max):
    """Given x, return y that linearly interpolates between two points
    (x_min, y_min) and (x_max, y_max)"""
    return y_min + (y_max - y_min) * (x - x_min) / (x_max - x_min)


def clamp(value, min_value, max_value):
    """Clamp value between min_value and max_value"""
    return max(min_value, min(value, max_value))


def extract_command_id_from_query(query: str) -> tuple[str, int]:
    """Extract the command ID from the query, if any"""
    try:
        return "", command_ids[query]
    except KeyError:
        pass
    try:
        command, actual_query = query.split(" ", maxsplit=1)
        return actual_query, command_ids[command]
    except (ValueError, KeyError):
        return query, DEFAULT_MODE_ID


def parse_query(query: str, mode="normal"):
    """
    Parse the query to extract search params, if any.

    Args:
        query (str): The query to parse.
        mode (str): The mode to use for parsing. Can be "normal" or "strict".
            In either mode, if the query ends with a JSON object, it is treated
            as search params and removed from the query. In "normal" mode, if it
            does not end with a JSON object but contains substrings in quotes,
            those substrings are treated as search params (but NOT removed from
            the query), namely as substrings that retrieved documents must contain.
    """

    if query.rstrip().endswith("}"):
        tmp_query = query.rstrip()
        # Find the corresponding opening brace
        brace_count = 0
        for i, char in enumerate(reversed(tmp_query)):
            if char == "}":
                brace_count += 1
            elif char == "{":
                brace_count -= 1
            if brace_count == 0:
                # Extract the JSON string at the end and set it as search params
                try:
                    search_params = json.loads(tmp_query[-i - 1 :])
                    query = tmp_query[: -i - 1]
                    return query, search_params
                except json.decoder.JSONDecodeError:
                    break

    # If we're here, the query does not end with a valid JSON object
    if mode != "normal":
        return query, {}

    # We are in "normal" mode with no JSON object at the end
    # Find all substrings in quotes and treat them as must-have substrings
    substrings_in_quotes = re.findall(r'"([^"]*)"', query)
    # substrings_in_quotes = re.findall(r'"(.*?)"', query) # another way (no '\n')

    # Remove empty strings and duplicates, form a list of $contains filters
    filters = [{"$contains": substr} for substr in set(substrings_in_quotes) if substr]

    # Combine the filters into a single $and filter and return results
    if not filters:
        return query, {}

    if len(filters) == 1:
        return query, {"where_document": filters[0]}

    return query, {"where_document": {"$and": filters}}


def utc_timestamp_int() -> int:
    """Returns the current UTC timestamp as an integer (seconds since epoch)"""
    return int(datetime.utcnow().timestamp())
