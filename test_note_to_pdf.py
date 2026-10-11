"""
Tests for note_to_pdf.py.
"""

import os
import re
import sqlite3
import struct

import pytest

import note_to_pdf


def make_jpeg(width, height, dpi = None):
    """
    Make the bytes of a minimal JPEG-like file.

    It has just enough structure (start marker, optional JFIF header, frame
    header and end marker) for its size and density to be read. It isn't a
    viewable image.

    ## Parameters

    - `width`: Width in pixels
    - `height`: Height in pixels
    - `dpi`: Dots per inch to put in a JFIF header, or `None` for no header

    ## Returns

    The file's bytes.
    """
    data = b"\xff\xd8"
    if dpi is not None:
        jfif  = b"JFIF\x00\x01\x01" + struct.pack(">BHHBB", 1, dpi, dpi, 0, 0)
        data += b"\xff\xe0" + struct.pack(">H", len(jfif) + 2) + jfif
    sof   = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00" * 3
    data += b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    data += b"\xff\xd9"
    return data


def make_database(path):
    """
    Make a small database shaped like Evernote's, with one notebook and notes.

    ## Parameters

    - `path`: Where to create the database file

    ## Returns

    The path, for convenience.
    """
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE Nodes_Notebook (id TEXT, label TEXT);
        CREATE TABLE Nodes_Note (id TEXT, label TEXT, parent_Notebook_id TEXT, deleted INTEGER);
        CREATE TABLE Attachment (id TEXT, mime TEXT, isActive INTEGER, dataHash TEXT, parent_Note_id TEXT);

        INSERT INTO Nodes_Notebook VALUES ('nb1', 'Written notebook');
        INSERT INTO Nodes_Notebook VALUES ('nb2', 'Other');

        INSERT INTO Nodes_Note VALUES ('note-1', 'Scans', 'nb1', NULL);
        INSERT INTO Nodes_Note VALUES ('note-2', 'Twin', 'nb1', NULL);
        INSERT INTO Nodes_Note VALUES ('note-3', 'Twin', 'nb1', NULL);
        INSERT INTO Nodes_Note VALUES ('note-4', 'Binned', 'nb1', 12345);
        INSERT INTO Nodes_Note VALUES ('note-5', 'Scans', 'nb2', NULL);

        INSERT INTO Attachment VALUES ('a1', 'image/jpeg', 1, 'hash-b', 'note-1');
        INSERT INTO Attachment VALUES ('a2', 'image/jpeg', 1, 'hash-a', 'note-1');
        INSERT INTO Attachment VALUES ('a3', 'image/jpeg', 0, 'hash-old', 'note-1');
        INSERT INTO Attachment VALUES ('a4', 'application/pdf', 1, 'hash-pdf', 'note-1');
    """)
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def connection(tmp_path):
    db_path    = make_database(str(tmp_path / "UDB-User123+RemoteGraph.sql"))
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


# ---- Finding the note and its images

def test_find_note_returns_id(connection):
    assert note_to_pdf.find_note(connection, "Written notebook", "Scans") == "note-1"


def test_find_note_unknown_notebook(connection):
    with pytest.raises(LookupError, match = "notebook"):
        note_to_pdf.find_note(connection, "Nope", "Scans")


def test_find_note_unknown_title(connection):
    with pytest.raises(LookupError, match = "No note"):
        note_to_pdf.find_note(connection, "Written notebook", "Nope")


def test_find_note_ignores_trash(connection):
    with pytest.raises(LookupError, match = "No note"):
        note_to_pdf.find_note(connection, "Written notebook", "Binned")


def test_find_note_ambiguous_title(connection):
    with pytest.raises(LookupError, match = "more than one"):
        note_to_pdf.find_note(connection, "Written notebook", "Twin")


def test_get_jpeg_hashes_only_active_jpegs(connection):
    hashes = note_to_pdf.get_jpeg_hashes(connection, "note-1")
    assert sorted(hashes) == ["hash-a", "hash-b"]


# ---- Putting the images in note order

def test_order_hashes_follows_content():
    content = (b"\x01en-media(\x00\x05\x04hash\x01w hash-a(\x00"
               b"\x01en-media(\x00\x05\x04hash\x01w hash-b(\x00"
               )
    hashes  = ["hash-b", "hash-a"]
    assert note_to_pdf.order_hashes(hashes, content) == ["hash-a", "hash-b"]


def test_order_hashes_skips_unknown_and_repeats():
    content = (b"en-media..hash..gone-hash "
               b"en-media..hash..hash-b "
               b"en-media..hash..hash-b "
               b"en-media..hash..hash-a "
               )
    hashes  = ["hash-a", "hash-b"]
    assert note_to_pdf.order_hashes(hashes, content) == ["hash-b", "hash-a"]


def test_order_hashes_puts_missing_ones_last():
    content = b"en-media..hash..hash-b "
    hashes  = ["hash-a", "hash-b", "hash-c"]
    assert note_to_pdf.order_hashes(hashes, content) == ["hash-b", "hash-a", "hash-c"]


def test_order_hashes_without_content_keeps_order():
    hashes = ["hash-a", "hash-b"]
    assert note_to_pdf.order_hashes(hashes, None) == hashes


# ---- Finding the files

def make_evernote_dirs(tmp_path):
    """
    Make a folder layout like Evernote's, returning the database path.
    """
    storage = tmp_path / "Evernote" / "conduit-storage" / "https%3A%2F%2Fwww.evernote.com"
    storage.mkdir(parents = True)
    return str(storage / "UDB-User123+RemoteGraph.sql")


def test_attachment_path(tmp_path):
    db_path  = make_evernote_dirs(tmp_path)
    expected = os.path.join(str(tmp_path), "Evernote", "resource-cache", "User123", "note-1", "hash-a")
    assert note_to_pdf.attachment_path(db_path, "note-1", "hash-a") == expected


def test_read_note_content_found(tmp_path):
    db_path = make_evernote_dirs(tmp_path)
    folder  = (tmp_path / "Evernote" / "conduit-fs" / "https%3A%2F%2Fwww.evernote.com" / "someuser"
               / "rte" / "Note" / "internal_rteDoc" / "abc" / "xyz")
    folder.mkdir(parents = True)
    (folder / "abcdef-xyz.dat").write_bytes(b"stuff")
    assert note_to_pdf.read_note_content(db_path, "abcdef-xyz") == b"stuff"


def test_read_note_content_missing(tmp_path):
    db_path = make_evernote_dirs(tmp_path)
    assert note_to_pdf.read_note_content(db_path, "abcdef-xyz") is None


# ---- Reading JPEGs

def test_jpeg_info_reads_size_and_dpi():
    assert note_to_pdf.jpeg_info(make_jpeg(640, 480, dpi = 300)) == (640, 480, 300, 3)


def test_jpeg_info_defaults_dpi():
    assert note_to_pdf.jpeg_info(make_jpeg(640, 480)) == (640, 480, note_to_pdf.DEFAULT_DPI, 3)


def test_jpeg_info_rejects_non_jpeg():
    with pytest.raises(ValueError, match = "not a JPEG"):
        note_to_pdf.jpeg_info(b"\x89PNG\r\n\x1a\n")


# ---- Making the PDF

def test_make_pdf_has_a_page_per_image():
    jpegs = [make_jpeg(100, 200, dpi = 72), make_jpeg(300, 150, dpi = 150)]
    pdf   = note_to_pdf.make_pdf(jpegs)

    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"/Count 2" in pdf
    assert b"/MediaBox [0 0 100 200]" in pdf
    assert b"/MediaBox [0 0 144 72]" in pdf
    assert jpegs[0] in pdf
    assert jpegs[1] in pdf


def test_make_pdf_xref_offsets_are_right():
    pdf = note_to_pdf.make_pdf([make_jpeg(10, 10), make_jpeg(20, 20)])

    startxref = int(re.search(rb"startxref\s+(\d+)", pdf).group(1))
    assert pdf[startxref:].startswith(b"xref")

    entries = re.findall(rb"(\d{10}) 00000 n", pdf[startxref:])
    for number, offset in enumerate(entries, start = 1):
        assert pdf[int(offset):].startswith(f"{number} 0 obj".encode())


# ---- Naming the file

def test_pdf_filename_plain():
    assert note_to_pdf.pdf_filename("2026-09-25") == "2026-09-25.pdf"


def test_pdf_filename_replaces_awkward_characters():
    assert note_to_pdf.pdf_filename('A/B: "c"?') == "A_B_ _c__.pdf"
