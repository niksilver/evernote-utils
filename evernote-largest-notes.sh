#!/usr/bin/env bash
#
# evernote-largest-notes.sh
#
# List the largest notes in Evernote, biggest first, by reading the
# local database that the Evernote desktop app (v10+) keeps on Windows.
#
# Usage:
#   ./evernote-largest-notes.sh          # show the top 25 notes
#   ./evernote-largest-notes.sh 100      # show the top 100 notes
#
# How it works
# ------------
# The Evernote desktop app keeps a local copy of your account in a
# SQLite database (the file ends ".sql", but it's an SQLite file).
# Two tables matter here:
#
#   Nodes_Note    One row per note. `content_size` is the size of the
#                 note's own text/markup in bytes, and `label` is its
#                 title.
#   Attachment    One row per attached file (PDF, image, etc). Each row
#                 holds `dataSize` in bytes, plus `parent_Note_id`
#                 (which note it belongs to).
#
# A note's total size = its own content_size + the dataSize of all its
# attachments. In practice the attachments are almost all of it.
#
# We copy the database to a temporary folder before querying it. That
# way we never touch the live file while Evernote is running.
#
# Caveats
# -------
# - Figures only cover what the desktop app has synced locally.
# - Evernote may change this database layout in future versions. If
#   the query fails, check the table/column names with:
#     sqlite3 <db file> '.schema Nodes_Note'
#     sqlite3 <db file> '.schema Attachment'
#
# Requires: sqlite3 (sudo apt install sqlite3)

set -euo pipefail

# ----------------------------------------------------------------------
# Settings you might want to change
# ----------------------------------------------------------------------

# How many notes to show. Takes the first command-line argument if
# given, or 25 if not.
LIMIT="${1:-25}"

# Evernote's data folder, as seen from WSL. This is the Windows folder
# %APPDATA%\Evernote\conduit-storage\https%3A%2F%2Fwww.evernote.com
EVERNOTE_DIR="/mnt/c/Users/nik/AppData/Roaming/Evernote/conduit-storage/https%3A%2F%2Fwww.evernote.com"

# The database file name. The number is your Evernote user ID. The
# similarly named "BackupRemoteGraph" file is an older backup, so we
# don't use it.
DB_NAME="UDB-User24342524+RemoteGraph.sql"

# Include notes that are in the Evernote trash? 0 = no, 1 = yes.
INCLUDE_TRASH=0

# Maximum number of characters of each note title to show. This keeps
# the columns readable.
TITLE_WIDTH=60

# ----------------------------------------------------------------------
# Copy the database somewhere safe
# ----------------------------------------------------------------------

if ! command -v sqlite3 >/dev/null; then
    echo "sqlite3 not found. Install it with: sudo apt install sqlite3" >&2
    exit 1
fi

if [[ ! -f "$EVERNOTE_DIR/$DB_NAME" ]]; then
    echo "Database not found: $EVERNOTE_DIR/$DB_NAME" >&2
    echo "Check EVERNOTE_DIR and DB_NAME at the top of this script." >&2
    exit 1
fi

# Make a temporary folder, and delete it whenever the script exits.
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Copy the main file plus its "-wal" and "-shm" companions, if present.
# Evernote writes recent changes to the -wal file first. Without it we
# might miss notes added or changed recently.
cp "$EVERNOTE_DIR/$DB_NAME"* "$TMP_DIR/"

# ----------------------------------------------------------------------
# Run the query
# ----------------------------------------------------------------------

# A note in the trash has a `deleted` timestamp. Live notes have NULL.
if [[ "$INCLUDE_TRASH" == 1 ]]; then
    TRASH_FILTER="1 = 1"                # Matches every note
else
    TRASH_FILTER="n.deleted IS NULL"    # Matches only notes not in the trash
fi

# 1048576 = bytes in one MB (1024 * 1024).
sqlite3 -header -column "$TMP_DIR/$DB_NAME" "
SELECT
    -- Total size: the note's text plus all its attachments, in MB.
    -- COALESCE turns 'no attachments' (NULL) into 0.
    printf('%.1f', (n.content_size + COALESCE(SUM(a.dataSize), 0)) / 1048576.0)
        AS total_mb,

    -- Just the attachments, in MB.
    printf('%.1f', COALESCE(SUM(a.dataSize), 0) / 1048576.0)
        AS attach_mb,

    -- Number of attachments on the note.
    COUNT(a.id) AS n_att,

    -- Which notebook the note is in.
    nb.label AS notebook,

    -- The note title, trimmed to TITLE_WIDTH characters.
    substr(n.label, 1, $TITLE_WIDTH) AS title

FROM Nodes_Note n

-- Attach each note's attachments. A LEFT JOIN keeps notes that have
-- none. 'isActive' leaves out attachments that have been removed from
-- the note.
LEFT JOIN Attachment a
    ON a.parent_Note_id = n.id
   AND a.isActive

-- Look up the notebook name.
LEFT JOIN Nodes_Notebook nb
    ON nb.id = n.parent_Notebook_id

WHERE $TRASH_FILTER

-- One output row per note, with its attachments added together.
GROUP BY n.id

-- Biggest first. This sorts on the exact byte count, not the rounded
-- MB figure.
ORDER BY n.content_size + COALESCE(SUM(a.dataSize), 0) DESC

LIMIT $LIMIT;
"
