#!/usr/bin/env python3
"""
List the titles of all the notes in an Evernote notebook.

This reads the local database kept by the Evernote desktop app (version 10
and later) on Windows. It does not talk to the Evernote servers, so it needs
no login or API key, but it only sees what the app has already synced.

The database is opened read-only, so it is safe to run this while Evernote
is open.

Usage:

    python3 list_note_titles.py                     # Lists 'Written notebook'
    python3 list_note_titles.py "Some other notebook"
    python3 list_note_titles.py --db /path/to/UDB-User123+RemoteGraph.sql

It works both from Windows itself and from WSL.
"""

import argparse
import glob
import os
import sqlite3
import sys
import urllib.parse


# The notebook to list if none is given on the command line
DEFAULT_NOTEBOOK = "Written notebook"

# Where Evernote keeps its local databases, relative to the Windows
# "AppData\Roaming" folder. The odd-looking folder name is the URL-encoded
# form of "https://www.evernote.com".
CONDUIT_STORAGE = os.path.join("Evernote",
                               "conduit-storage",
                               "https%3A%2F%2Fwww.evernote.com",
                               )

# The database holding the notes and notebooks. There is one per Evernote
# user, named after their user number, e.g. "UDB-User24342524+RemoteGraph.sql".
DB_PATTERN = "UDB-User*+RemoteGraph.sql"


def find_appdata_dirs():
    """
    Find the Windows "AppData\\Roaming" folder(s) that might hold Evernote data.

    ## Returns

    A list of candidate folder paths. On Windows this is just `%APPDATA%`.
    Under WSL it is the Roaming folder of every Windows user under
    `/mnt/c/Users`, since we can't be sure which Windows user is ours.
    """
    # Running natively on Windows: the APPDATA environment variable
    # points straight at the Roaming folder
    appdata = os.environ.get("APPDATA")
    if appdata:
        return [appdata]

    # Running under WSL: the Windows C: drive is mounted at /mnt/c
    return glob.glob("/mnt/c/Users/*/AppData/Roaming")


def find_database():
    """
    Find the Evernote database that holds the notes and notebooks.

    ## Returns

    The path of the database file.

    ## Raises

    - `FileNotFoundError`: If no database can be found, or if more than one
      is found (e.g. several Evernote accounts) so we can't tell which to use
    """
    matches = []
    for appdata in find_appdata_dirs():
        pattern  = os.path.join(appdata, CONDUIT_STORAGE, DB_PATTERN)
        matches += glob.glob(pattern)

    if len(matches) == 0:
        raise FileNotFoundError("Could not find the Evernote database. "
                                "Is the Evernote desktop app installed? "
                                "You can give its location with --db."
                                )
    if len(matches) > 1:
        raise FileNotFoundError("Found more than one Evernote database; "
                                "choose one with --db:\n  "
                                + "\n  ".join(matches)
                                )
    return matches[0]


def open_read_only(db_path):
    """
    Open an SQLite database in read-only mode.

    Read-only mode means we can't accidentally change anything, and we
    won't upset the Evernote app if it's running at the same time.

    ## Parameters

    - `db_path`: Path of the database file

    ## Returns

    An open `sqlite3.Connection`.
    """
    # Read-only mode needs the path given as a "file:" URI. The path must be
    # URL-encoded, partly because Evernote's folder name itself contains "%"
    # characters which would otherwise be misread.
    uri = "file:" + urllib.parse.quote(db_path) + "?mode=ro"
    return sqlite3.connect(uri, uri = True)


def get_note_titles(connection, notebook_name):
    """
    Get the titles of all the notes in the named notebook.

    Notes that are in the trash are left out.

    ## Parameters

    - `connection`: An open connection to the Evernote database
    - `notebook_name`: Name of the notebook, exactly as shown in Evernote

    ## Returns

    A list of note titles, sorted alphabetically (ignoring case).

    ## Raises

    - `LookupError`: If there is no notebook with that name
    """
    # First check the notebook exists, so we can tell "no such notebook"
    # apart from "notebook with no notes in it". In the database the
    # notebook's name is held in its "label" column.
    notebook_rows = connection.execute("SELECT id FROM Nodes_Notebook WHERE label = ?",
                                       (notebook_name,),
                                       ).fetchall()
    if not notebook_rows:
        raise LookupError(f"No notebook called '{notebook_name}'")

    # Each note records which notebook it's in via parent_Notebook_id, and
    # again its title is in the "label" column. A note in the trash has a
    # "deleted" timestamp; other notes have NULL there.
    notebook_ids = [row[0] for row in notebook_rows]
    placeholders = ", ".join("?" for _ in notebook_ids)
    query        = (f"SELECT label FROM Nodes_Note"
                    f" WHERE parent_Notebook_id IN ({placeholders})"
                    f" AND deleted IS NULL"
                    )
    titles = [row[0] for row in connection.execute(query, notebook_ids)]

    return sorted(titles, key = str.casefold)


def parse_args():
    """
    Read the command-line arguments.

    ## Returns

    An `argparse.Namespace` with `notebook` and `db` attributes.
    """
    parser = argparse.ArgumentParser(description = "List the titles of the notes in an Evernote notebook.")
    parser.add_argument("notebook",
                        nargs   = "?",
                        default = DEFAULT_NOTEBOOK,
                        help    = f"Name of the notebook (default: '{DEFAULT_NOTEBOOK}')",
                        )
    parser.add_argument("--db",
                        help = "Path to the Evernote RemoteGraph.sql database (found automatically if omitted)",
                        )
    return parser.parse_args()


def main():
    """
    Print the title of each note in the notebook, one per line.
    """
    args = parse_args()

    try:
        db_path    = args.db or find_database()
        connection = open_read_only(db_path)
        titles     = get_note_titles(connection, args.notebook)
    except (FileNotFoundError, LookupError, sqlite3.Error) as e:
        print(f"Error: {e}", file = sys.stderr)
        sys.exit(1)

    for title in titles:
        print(title)


if __name__ == "__main__":
    main()
