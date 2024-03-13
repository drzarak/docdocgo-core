import os

from chromadb import Collection

from components.chroma_ddg import ChromaDDG
from utils.chat_state import ChatState
from utils.helpers import (
    DB_COMMAND_HELP_TEMPLATE,
    PRIVATE_COLLECTION_FULL_PREFIX_LENGTH,
    PRIVATE_COLLECTION_PREFIX,
    PRIVATE_COLLECTION_PREFIX_LENGTH,
    PRIVATE_COLLECTION_USER_ID_LENGTH,
    format_nonstreaming_answer,
)
from utils.input import get_choice_from_dict_menu, get_menu_choice
from utils.output import format_exception
from utils.prepare import DEFAULT_COLLECTION_NAME
from utils.query_parsing import DBCommand
from utils.type_utils import (
    AccessRole,
    CollectionUserSettings,
    OperationMode,
    Props,
)

menu_main = {
    DBCommand.LIST: "List collections",
    DBCommand.USE: "Switch collection",
    DBCommand.RENAME: "Rename collection",
    DBCommand.DELETE: "Delete collection",
    DBCommand.EXIT: "I'm done here",
}


def get_native_owner_user_id(collection_name: str) -> str | None:
    """
    Get the user ID of the native owner of a collection. If the collection is public,
    return None.
    """
    if collection_name.startswith(PRIVATE_COLLECTION_PREFIX):
        return collection_name[
            PRIVATE_COLLECTION_PREFIX_LENGTH:PRIVATE_COLLECTION_FULL_PREFIX_LENGTH
        ]


ALWAYS_REMOVE_PREFIX = "#=-, !"  # string that can't be a user id


def get_user_facing_collection_name(user_id: str | None, collection_name: str) -> str:
    """
    Get the user-facing name of a collection by removing the internal prefix
    containing the user ID, if any. The prefix is removed only if the user ID
    matches the one in the collection name.
    """
    # Old collections: u-abcdef<name>, new collections: u-abcdef-<name>
    return (
        collection_name[PRIVATE_COLLECTION_FULL_PREFIX_LENGTH:].lstrip("-")
        if collection_name.startswith(PRIVATE_COLLECTION_PREFIX)
        and user_id == get_native_owner_user_id(collection_name)
        else collection_name
    )


def construct_full_collection_name(user_id: str | None, collection_name: str) -> str:
    """
    Construct the full collection name from the user ID and the user-facing name.
    """
    return (
        f"{PRIVATE_COLLECTION_PREFIX}{user_id[-PRIVATE_COLLECTION_USER_ID_LENGTH:]}"
        f"-{collection_name}"
        if user_id
        else collection_name
    )


def is_user_authorized_for_collection(
    chat_state: ChatState,
    coll_name_full: str | None = None,
    access_code: str | None = None,
) -> bool:
    """
    Check if the user is authorized to access the given collection.
    """
    coll_name_full = coll_name_full or chat_state.vectorstore.name

    # Public collections are always accessible
    if not coll_name_full.startswith(PRIVATE_COLLECTION_PREFIX):
        return True

    # A private collection is accessible if it's the user's own collection
    if chat_state.user_id and coll_name_full.startswith(
        PRIVATE_COLLECTION_PREFIX
        + chat_state.user_id[-PRIVATE_COLLECTION_USER_ID_LENGTH:]
    ):
        return True

    # If can't be authorized with the simple checks above, check the collection's metadata
    collection_permissions = chat_state.get_collection_permissions(coll_name_full)
    print(f"collection_permissions: {collection_permissions}")
    if (
        collection_permissions.get_user_settings(chat_state.user_id)
        == AccessRole.EDITOR
    ):
        return True

    code_settings = collection_permissions.get_access_code_settings(access_code)
    if code_settings.access_role == AccessRole.EDITOR:
        return True
    # TODO: implement the NEED_ONCE case

    return False


def get_access_role(
    chat_state: ChatState,
    coll_name_full: str | None = None,
    access_code: str | None = None,
) -> AccessRole:
    """
    Get the access status for the current user to the current or specified collection.
    """
    # TODO: can probably eliminate the need for fetching metadata in cases where
    # all we need is the user's access role to be viewer and that's already stored in
    # chat_state.
    coll_name_full = coll_name_full or chat_state.vectorstore.name

    # The default collection is always accessible in read-only mode
    if coll_name_full == DEFAULT_COLLECTION_NAME:
        admin_pwd = os.getenv("BYPASS_SETTINGS_RESTRICTIONS_PASSWORD")
        if admin_pwd and access_code == admin_pwd:
            return AccessRole.OWNER
        return AccessRole.VIEWER

    # Public collections are always accessible
    if not coll_name_full.startswith(PRIVATE_COLLECTION_PREFIX):
        return AccessRole.OWNER

    # Check if it's the user's own collection
    if chat_state.user_id and coll_name_full.startswith(
        PRIVATE_COLLECTION_PREFIX
        + chat_state.user_id[-PRIVATE_COLLECTION_USER_ID_LENGTH:]
    ):
        return AccessRole.OWNER

    # If access code was used preiously, retrieve access role from chat_state
    stored_access_role = chat_state.access_role_by_user_id_by_coll.get(
        coll_name_full, {}
    ).get(chat_state.user_id, AccessRole.NONE)

    # If no access code is being used, trust the stored access role to avoid fetching
    # metadata. It's possible that a higher role was assigned to the user during this
    # ession, but it's not worth the extra request to the server to check, since the
    # user can always reload the page to get a new session.
    if stored_access_role != AccessRole.NONE and access_code is None:
        return stored_access_role

    # If can't be authorized with the simple checks above, check the collection's metadata
    collection_permissions = chat_state.get_collection_permissions(coll_name_full)
    print(f"collection_permissions: {collection_permissions}")

    user_settings = collection_permissions.get_user_settings(chat_state.user_id)
    code_settings = collection_permissions.get_access_code_settings(access_code)

    # Determine the highest access role available
    role = max(
        code_settings.access_role,
        user_settings.access_role,
        stored_access_role,
        key=lambda x: x.value,
    )

    # Store the access role in chat_state for future use within the same session
    if role.value > stored_access_role.value:
        chat_state.access_role_by_user_id_by_coll.setdefault(coll_name_full, {})[
            chat_state.user_id
        ] = role

    return role


GET_ALL = "GET_ALL"


def get_collections(
    vectorstore: ChromaDDG, user_id: str | None, include_default_collection=True
) -> list[Collection]:
    """
    Get the collections for the given user. If `user_id` is None, return only public
    collections. If `include_default_collection` is False, don't return the default
    collection. If `user_id` is GET_ALL, return all collections.
    """
    collections = vectorstore.client.list_collections()
    if not user_id:
        # Return only public collections
        return [
            c for c in collections if not c.name.startswith(PRIVATE_COLLECTION_PREFIX)
        ]

    if user_id == GET_ALL:
        return collections

    if len(user_id) < PRIVATE_COLLECTION_USER_ID_LENGTH:
        raise ValueError(f"Invalid user_id: {user_id}")

    # User's collections are prefixed with:
    prefix = PRIVATE_COLLECTION_PREFIX + user_id[-PRIVATE_COLLECTION_USER_ID_LENGTH:]

    if not include_default_collection:
        # Return only the user's collections
        return [c for c in collections if c.name.startswith(prefix)]

    # Return the user's collections and the default collection
    return [
        c
        for c in collections
        if c.name.startswith(prefix) or c.name == DEFAULT_COLLECTION_NAME
    ]


def manage_dbs_console(chat_state: ChatState) -> Props:
    """
    Manage collections from the console (using `input`).
    NOTE: In console mode, there's no separation of users.
    """
    while True:
        # Print the menu and get the user's choice
        print()
        command = get_choice_from_dict_menu(menu_main)
        if command == DBCommand.EXIT:
            print("OK, back to the chat.")
            return {"answer": ""}
        elif command == DBCommand.LIST:
            collections = chat_state.vectorstore.client.list_collections()
            print("\nAvailable collections:")
            for i, collection in enumerate(collections):
                print(f"{i+1}. {collection.name}")
        elif command == DBCommand.USE:
            collections = chat_state.vectorstore.client.list_collections()
            collection_names = [collection.name for collection in collections]
            print()
            collection_idx = get_menu_choice(
                collection_names,
                "Enter the number corresponding to the collection you want "
                "to switch to (Enter = Cancel): ",
                default=-1,
            )
            if collection_idx != -1:
                collection_name = collection_names[collection_idx]
                print(f"Switching to collection: {collection_name}")
                return {
                    "answer": "",
                    "vectorstore": chat_state.get_new_vectorstore(collection_name),
                }
        elif command == DBCommand.RENAME:
            if chat_state.vectorstore.name == DEFAULT_COLLECTION_NAME:
                print("You cannot rename the default collection.")
                continue
            print(f"The current collection name is: {chat_state.vectorstore.name}")
            new_name = input(
                "Enter the new name for this collection (Enter = Cancel): "
            )
            if not new_name:
                continue
            try:
                chat_state.vectorstore.rename_collection(new_name)
            except Exception as e:
                print(f"Error renaming collection: {e}")
                continue
            print(f"Collection renamed to: {new_name}")
            return {
                "answer": "",
                "vectorstore": chat_state.get_new_vectorstore(new_name),
            }  # NOTE: can likely just return vectorstore without reinitializing
        elif command == DBCommand.DELETE:
            collections = chat_state.vectorstore.client.list_collections()
            collection_names = [collection.name for collection in collections]
            print()
            collection_idx = get_menu_choice(
                collection_names,
                "Enter the number corresponding to the collection you want "
                "to delete (Enter = Cancel): ",
                default=-1,
            )
            if collection_idx == -1:
                continue
            collection_name = collection_names[collection_idx]
            if collection_name == DEFAULT_COLLECTION_NAME:
                print("You cannot delete the default collection.")
                continue
            if collection_name == chat_state.vectorstore.name:
                print("You cannot delete the currently selected collection.")
                continue
            ans = input(
                f"Are you sure you want to delete the collection {collection_name}? [y/N] "
            )
            if ans == "y":
                chat_state.vectorstore.delete_collection(collection_name)
                print(f"Collection {collection_name} deleted.")
                return {"answer": ""}


def sort_collection_names(
    coll_names_as_shown: list[str], coll_names_full: list[str]
) -> tuple[list[str], list[str]]:
    # Sort the collections by name as shown, but put the default collection first
    coll_name_pairs = sorted(
        zip(coll_names_as_shown, coll_names_full),
        key=lambda pair: (pair[1] != DEFAULT_COLLECTION_NAME, pair[0]),
    )
    return tuple(zip(*coll_name_pairs))


def handle_db_command_with_subcommand(chat_state: ChatState) -> Props:
    command = chat_state.parsed_query.db_command
    value = chat_state.parsed_query.message

    collections = get_collections(chat_state.vectorstore, chat_state.user_id)
    coll_names_full = [c.name for c in collections]
    coll_names_as_shown = [
        get_user_facing_collection_name(chat_state.user_id, n) for n in coll_names_full
    ]
    coll_names_as_shown, coll_names_full = sort_collection_names(
        coll_names_as_shown, coll_names_full
    )

    def get_available_dbs_str() -> str:
        tmp = "\n".join([f"{i+1}. {n}" for i, n in enumerate(coll_names_as_shown)])
        return f"Available collections:\n\n{tmp}"

    def get_db_not_found_str(name: str) -> str:
        return (
            f"Collection {name} doesn't exist or you don't have owner access to it. "
            f"{get_available_dbs_str()}"
        )

    admin_pwd = os.getenv("BYPASS_SETTINGS_RESTRICTIONS_PASSWORD")

    if command == DBCommand.LIST:
        if value == admin_pwd:
            all_collections = get_collections(chat_state.vectorstore, GET_ALL)
            all_coll_names_full = [c.name for c in all_collections]
            tmp = "\n".join([f"{i+1}. {n}" for i, n in enumerate(all_coll_names_full)])
            return format_nonstreaming_answer(
                f"Full collection names for all users:\n\n{tmp}"
            )

        return format_nonstreaming_answer(
            f"{get_available_dbs_str()}\n\n"
            "**Tip:** To switch to collection number N, type `/db use N`."
        )

    if command == DBCommand.USE:
        if not value:
            return format_nonstreaming_answer(
                get_available_dbs_str()
                + "\n\nTo switch collections, you must provide the name or number "
                "of the collection to switch to. Example:\n"
                "```\n/db use 3\n```"
            )

        # Get the name of the collection to switch to
        try:
            idx = coll_names_as_shown.index(value)
            coll_name_to_show = value
            coll_name_full = coll_names_full[idx]
        except ValueError:
            try:
                # See if the user provided an index directly instead of a name
                idx = int(value) - 1
                if idx < 0 or idx >= len(coll_names_as_shown):
                    raise ValueError
                coll_name_to_show = coll_names_as_shown[idx]
                coll_name_full = coll_names_full[idx]
            except ValueError:
                # See if it's a non-native collection (shared with user)
                if get_access_role(chat_state, value) == AccessRole.NONE:
                    return format_nonstreaming_answer(get_db_not_found_str(value))
                coll_name_to_show = coll_name_full = value

        vectorstore = chat_state.get_new_vectorstore(coll_name_full)
        # NOTE: we are loading the same vectorstore twice if we used get_access_role
        return format_nonstreaming_answer(
            f"Switched to collection: `{coll_name_to_show}`."
        ) | {"vectorstore": vectorstore}

    if command == DBCommand.RENAME:
        if not value:
            return format_nonstreaming_answer(
                "To rename the current collection, you must provide a new name. Example:\n"
                "```\n/db rename awesome-new-name\n```"
            )

        if chat_state.vectorstore.name == DEFAULT_COLLECTION_NAME:
            return format_nonstreaming_answer(
                "You cannot rename the default collection."
            )

        # Admin can rename to the default collection's name by providing the password.
        # If it's not a special admin command, check if the user has editor access
        if value == f"--default {admin_pwd}" and admin_pwd:
            # Before renaming, we need to delete the default collection if it exists
            chat_state.vectorstore.delete_collection(DEFAULT_COLLECTION_NAME)
            value = DEFAULT_COLLECTION_NAME
        elif get_access_role(chat_state).value < AccessRole.OWNER.value:
            return format_nonstreaming_answer(
                "You can't rename the current collection because you "
                "don't have owner access to it."
            )

        # From this point on, the user has owner access to the collection

        # Get the full name of the collection to rename to
        native_owner_user_id = get_native_owner_user_id(chat_state.vectorstore.name)
        if value == DEFAULT_COLLECTION_NAME:
            # Will usually fail, but ok if admin has deleted the default collection
            new_full_name = DEFAULT_COLLECTION_NAME
        elif native_owner_user_id is None:
            # Public collection remains public
            new_full_name = value
            if new_full_name.startswith(PRIVATE_COLLECTION_PREFIX):
                return format_nonstreaming_answer(
                    f"A public collection's name cannot start with `{PRIVATE_COLLECTION_PREFIX}`."
                )
        else:
            new_full_name = construct_full_collection_name(chat_state.user_id, value)

        # Rename the collection
        try:
            chat_state.vectorstore.rename_collection(new_full_name)
        except Exception as e:
            return format_nonstreaming_answer(
                f"Error renaming collection:\n```\n{format_exception(e)}\n```"
            )

        # Check if collection was taken away from the original owner and restore their access
        if native_owner_user_id != chat_state.user_id:
            chat_state.save_collection_settings_for_user(
                native_owner_user_id,
                CollectionUserSettings(access_role=AccessRole.OWNER),
            )
            print(f"Restored owner access to {native_owner_user_id}")

        # Get vectorstore with updated name, form and return the answer
        return format_nonstreaming_answer(f"Collection renamed to `{value}`.") | {
            "vectorstore": chat_state.get_new_vectorstore(new_full_name),
        }

    if command == DBCommand.DELETE:
        if not value:
            return format_nonstreaming_answer(
                get_available_dbs_str()
                + "\n\nTo delete a collection, you must provide the name of "
                "the collection to delete, a list of collection numbers, or "
                "the --current (-c) flag to delete the current collection. Examples:\n"
                "```\n/db delete my-temp-db\n/db delete 2, 4, 19\n"
                "/db delete 19\n/db delete -c\n```"
            )

        full_names = None

        if value == "-c" or value == "--current":
            if chat_state.vectorstore.name == DEFAULT_COLLECTION_NAME:
                value = DEFAULT_COLLECTION_NAME  # will return an error below
            else:
                full_names = [chat_state.vectorstore.name]

        if value == DEFAULT_COLLECTION_NAME:
            return format_nonstreaming_answer(
                "You cannot delete the default collection."
            )

        # Admin can delete the default collection by providing the password
        if value == f"--default {admin_pwd}" and admin_pwd:
            full_names = [DEFAULT_COLLECTION_NAME]

        # NOTE: the functionality below requires allow_reset=True in the settings
        # or an ALLOW_RESET env variable **on the server**.

        # # Admin can also reset the whole db by providing the password
        # if value == f"--reset {pwd}" and pwd:
        #     chat_state.vectorstore.client.reset()
        #     return format_nonstreaming_answer("The entire database has been reset.")

        # Get the full name(s) of the collection(s) to delete
        try:
            if full_names is None:
                full_names = [coll_names_full[coll_names_as_shown.index(value)]]
                # NOTE: there's a small chance of an ambiguity if the user has
                # a collection with the same name as a public collection, or if
                # they have their own collection with the as-shown name of
                # "u-<some other user's id>-<some other user's collection name>".
                # In both cases, the name will be resolved to the user's own collection.
        except ValueError:
            try:
                # See if the user provided index(es) directly instead of a name
                # NOTE: this takes precedence over non-native collection name such as
                # "123" (which could be a public collection name), but that's ok, we don't
                # want to block the user from deleting a collection by its index and delete
                # a public collection by mistake.
                # NOTE: could prohibit collection names that are just numbers
                if "-" in value:
                    # Admin can delete a range of collections by providing the password
                    if not value.endswith(f" {admin_pwd}") or not admin_pwd:
                        raise ValueError
                    leftright = value[: -len(admin_pwd) - 1].split("-")
                    if len(leftright) != 2:
                        raise ValueError
                    min_idx, max_idx = int(leftright[0]) - 1, int(leftright[1]) - 1
                    if min_idx < 1 or max_idx >= len(coll_names_as_shown):
                        raise ValueError
                    idxs = list(range(min_idx, max_idx + 1))
                else:
                    # Usual case: see if we got a comma-separated list of indexes
                    idxs = [int(s) - 1 for s in value.split(",")]

                    # Check that all idxs are valid
                    if any(idx < 1 or idx >= len(coll_names_as_shown) for idx in idxs):
                        raise ValueError  # idx == 0 not allowed, it's the default collection

                # One last check:
                if not idxs:
                    raise ValueError

                # Get the full names of the collections
                full_names = [coll_names_full[idx] for idx in idxs]
            except ValueError:
                # See if it's a non-native collection (shared with user)
                # That's the only case where we need to check access role
                if get_access_role(chat_state, value).value < AccessRole.OWNER.value:
                    # NOTE: could have a separate NOT_EXIST "role" if we want to distinguish
                    # between not found and not accessible even as a viewer
                    return format_nonstreaming_answer(get_db_not_found_str(value))
                full_names = [value]

        # Delete the collection(s)
        for full_name in full_names:
            chat_state.vectorstore.delete_collection(full_name)
            # NOTE: could stream progress here

        # Form answer, and - if the current collection was deleted - initiate a switch
        names_as_shown = [
            get_user_facing_collection_name(chat_state.user_id, x) for x in full_names
        ]
        s_or_no_s = "s" if len(names_as_shown) > 1 else ""
        ans = format_nonstreaming_answer(
            f"Collection{s_or_no_s} `{', '.join(names_as_shown)}` deleted."
        )
        if any(full_name == chat_state.vectorstore.name for full_name in full_names):
            ans["vectorstore"] = chat_state.get_new_vectorstore(DEFAULT_COLLECTION_NAME)

        return ans

    # Should never happen
    raise ValueError(f"Invalid /db subcommand: {command}")

    # Currently unused logic for switching to a db in a different directory

    # try:
    #     db_dir, collection_name = os.path.split(value)
    # except Exception:
    #     db_dir = collection_name = ""
    # if not collection_name:
    #     return partial_res | {"answer": "A valid docs db name must be provided."}
    # try:
    #     if db_dir:
    #         chroma_client = initialize_client(db_dir)
    #     else:
    #         chroma_client = vectorstore._client
    #     vectorstore = load_vectorstore(collection_name, chroma_client)
    # except Exception as e:
    #     print(f"Error loading requested collection: {e}")
    #     return {"answer": ""}
    # print(f"Switching to vector collection: {message}")
    # return {
    #     "answer": "",
    #     "vectorstore": vectorstore,
    # }


def handle_db_command(chat_state: ChatState) -> Props:
    """
    Handle a /db command
    """

    # Handle /db with no valid subcommand
    if chat_state.parsed_query.db_command == DBCommand.NONE:
        if chat_state.operation_mode.value == OperationMode.CONSOLE.value:
            return manage_dbs_console(chat_state)
        return format_nonstreaming_answer(
            DB_COMMAND_HELP_TEMPLATE.format(current_db=chat_state.vectorstore.name)
        )

    # Handle the command
    return handle_db_command_with_subcommand(chat_state)
