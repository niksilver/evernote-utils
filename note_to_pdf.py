#!/usr/bin/env python3
"""
Turn the JPEG images in an Evernote note into a PDF, one image per page.

The PDF is named after the note, e.g. a note called "2026-09-25" becomes
"2026-09-25.pdf". The images appear in the same order as in the note. The
JPEG data is put into the PDF as-is, so there is no loss of quality.

Like list_note_titles.py, this reads the local data kept by the Evernote
desktop app (version 10 and later) on Windows, read-only. It needs no login,
but the note's images must already have been downloaded by the app. If they
haven't, opening the note in Evernote will fetch them.

Usage:

    python3 note_to_pdf.py "Written notebook" "2026-09-25"
    python3 note_to_pdf.py "Written notebook" "2026-09-25" --output-dir ~/scans
    python3 note_to_pdf.py "Written notebook" "2026-09-25" --db /path/to/UDB-User123+RemoteGraph.sql

It works both from Windows itself and from WSL.
"""

import argparse
import glob
import os
import re
import sqlite3
import struct
import sys

import list_note_titles


# Resolution to assume for an image that doesn't say what its resolution is.
# It decides the page size: at 72 dots per inch, one pixel is one PDF point.
DEFAULT_DPI = 72

# Characters that aren't allowed in Windows filenames (which are stricter
# than Linux ones), so can't be used in the PDF's name
BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def find_note(connection, notebook_name, note_title):
    """
    Find the note with the given title in the named notebook.

    Notes in the trash are ignored.

    ## Parameters

    - `connection`: An open connection to the Evernote database
    - `notebook_name`: Name of the notebook, exactly as shown in Evernote
    - `note_title`: Title of the note, exactly as shown in Evernote

    ## Returns

    The note's ID.

    ## Raises

    - `LookupError`: If there is no such notebook, no such note, or more than
      one note with that title
    """
    notebook_ids = [row[0] for row in connection.execute("SELECT id FROM Nodes_Notebook WHERE label = ?",
                                                         (notebook_name,),
                                                         )]
    if not notebook_ids:
        raise LookupError(f"No notebook called '{notebook_name}'")

    placeholders = ", ".join("?" for _ in notebook_ids)
    query        = (f"SELECT id FROM Nodes_Note"
                    f" WHERE parent_Notebook_id IN ({placeholders})"
                    f" AND label = ?"
                    f" AND deleted IS NULL"
                    )
    note_ids = [row[0] for row in connection.execute(query, notebook_ids + [note_title])]

    if not note_ids:
        raise LookupError(f"No note called '{note_title}' in notebook '{notebook_name}'")
    if len(note_ids) > 1:
        raise LookupError(f"There is more than one note called '{note_title}'"
                          f" in notebook '{notebook_name}'"
                          )
    return note_ids[0]


def get_jpeg_hashes(connection, note_id):
    """
    Get the JPEG images attached to a note.

    Evernote identifies each attachment's data by its MD5 hash, which is
    also how the note's content refers to it. Attachments that have been
    removed from the note are left out.

    ## Parameters

    - `connection`: An open connection to the Evernote database
    - `note_id`: ID of the note

    ## Returns

    A list of the images' hashes, in no particular order.
    """
    rows = connection.execute("SELECT dataHash FROM Attachment"
                              " WHERE parent_Note_id = ?"
                              " AND mime = 'image/jpeg'"
                              " AND isActive = 1",
                              (note_id,),
                              )
    return [row[0] for row in rows]


def order_hashes(hashes, content):
    """
    Put image hashes into the order the images appear in the note.

    The note's content is a binary file, but each image in it shows up as an
    "en-media" element followed closely by the image's hash. So we take the
    hashes in the order they first appear after an "en-media".

    ## Parameters

    - `hashes`: The hashes of the note's images
    - `content`: The bytes of the note's content, or `None` if it isn't known

    ## Returns

    The hashes in note order. Any that can't be found in the content come
    last, in the order they were given.
    """
    if content is None:
        return list(hashes)

    # Try longer hashes first, in case one is the start of another
    alternatives = b"|".join(re.escape(h.encode()) for h in sorted(hashes, key = len, reverse = True))
    pattern      = re.compile(rb"en-media.{0,40}?hash.{0,10}?(" + alternatives + rb")", re.DOTALL)

    ordered = []
    for match in pattern.finditer(content):
        found = match.group(1).decode()
        if found not in ordered:
            ordered.append(found)

    return ordered + [h for h in hashes if h not in ordered]


def evernote_dir(db_path):
    """
    Get the Evernote app's data folder from the path of its database.

    The database lives in `<Evernote>/conduit-storage/<server>/`.

    ## Parameters

    - `db_path`: Path of the RemoteGraph.sql database

    ## Returns

    The path of the Evernote data folder.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(db_path))))


def attachment_path(db_path, note_id, data_hash):
    """
    Get where the Evernote app keeps its downloaded copy of an attachment.

    That's `<Evernote>/resource-cache/User<number>/<note ID>/<hash>`, where
    the user number is the one in the database's filename.

    ## Parameters

    - `db_path`: Path of the RemoteGraph.sql database
    - `note_id`: ID of the note the attachment belongs to
    - `data_hash`: Hash of the attachment's data

    ## Returns

    The path of the file. It may not exist if the app hasn't downloaded it.
    """
    user = re.match(r"UDB-(User\d+)\+", os.path.basename(db_path)).group(1)
    return os.path.join(evernote_dir(db_path), "resource-cache", user, note_id, data_hash)


def read_note_content(db_path, note_id):
    """
    Read the Evernote app's local copy of a note's content.

    It's kept in
    `<Evernote>/conduit-fs/<server>/<username>/rte/Note/internal_rteDoc/...`,
    in subfolders named after the first and last three characters of the
    note's ID.

    ## Parameters

    - `db_path`: Path of the RemoteGraph.sql database
    - `note_id`: ID of the note

    ## Returns

    The content as bytes, or `None` if there's no local copy.
    """
    server  = os.path.basename(os.path.dirname(os.path.abspath(db_path)))
    pattern = os.path.join(glob.escape(evernote_dir(db_path)),
                           "conduit-fs",
                           glob.escape(server),
                           "*",
                           "rte",
                           "Note",
                           "internal_rteDoc",
                           glob.escape(note_id[:3]),
                           glob.escape(note_id[-3:]),
                           glob.escape(note_id) + ".dat",
                           )
    matches = glob.glob(pattern)
    if not matches:
        return None

    with open(matches[0], "rb") as f:
        return f.read()


def jpeg_info(data):
    """
    Read the size, resolution and number of colour components of a JPEG.

    ## Parameters

    - `data`: The bytes of the JPEG file

    ## Returns

    A tuple `(width, height, dpi, components)`. The dpi is `DEFAULT_DPI` if
    the file doesn't give one. Components is 1 for greyscale, 3 for colour
    (RGB), or 4 for CMYK.

    ## Raises

    - `ValueError`: If the data is not a JPEG, or its size can't be found
    """
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("The data is not a JPEG")

    dpi = DEFAULT_DPI
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xff:
            break
        marker = data[pos + 1]

        # Padding, and markers with no data following them
        if marker == 0xff:
            pos += 1
            continue
        if marker == 0x01 or 0xd0 <= marker <= 0xd9:
            pos += 2
            continue

        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        segment   = data[pos + 4 : pos + 2 + length]

        # A JFIF header, which may give the resolution, either in dots per
        # inch (units 1) or dots per centimetre (units 2)
        if marker == 0xe0 and segment.startswith(b"JFIF\x00") and len(segment) >= 12:
            units, x_density = struct.unpack(">BH", segment[7:10])
            if units == 1 and x_density > 1:
                dpi = x_density
            elif units == 2 and x_density > 1:
                dpi = x_density * 2.54

        # A frame header, which gives the size. Markers C0 to CF are frame
        # headers, except for C4, C8 and CC which mean other things.
        if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
            _, height, width, components = struct.unpack(">BHHB", segment[:6])
            return (width, height, dpi, components)

        pos += 2 + length

    raise ValueError("Could not find the size of the JPEG")


def format_number(number):
    """
    Write a number for a PDF, with no more decimal places than needed.

    ## Parameters

    - `number`: The number

    ## Returns

    The number as a string, e.g. `"144"` or `"12.5"`.
    """
    return f"{number:.2f}".rstrip("0").rstrip(".")


def make_pdf(jpegs):
    """
    Make a PDF with one JPEG image per page.

    Each page is the size of its image at the image's resolution, and the
    JPEG data is embedded unchanged.

    ## Parameters

    - `jpegs`: A list of JPEG files' bytes, in page order

    ## Returns

    The bytes of the PDF file.

    ## Raises

    - `ValueError`: If any of the data is not a JPEG
    """
    colour_spaces = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}

    # Objects 1 and 2 are the catalog and the page list. Then each image
    # gets three objects: its page, the image, and the drawing instructions.
    objects   = [None, None]
    page_refs = []
    for jpeg in jpegs:
        width, height, dpi, components = jpeg_info(jpeg)
        page_width  = format_number(width * 72 / dpi)
        page_height = format_number(height * 72 / dpi)
        page_num    = len(objects) + 1
        image_num   = page_num + 1
        draw_num    = page_num + 2

        # CMYK JPEGs are nearly always made by Adobe software, which stores
        # the colours inverted
        decode = " /Decode [1 0 1 0 1 0 1 0]" if components == 4 else ""

        page  = (f"<< /Type /Page /Parent 2 0 R"
                 f" /MediaBox [0 0 {page_width} {page_height}]"
                 f" /Resources << /XObject << /Im0 {image_num} 0 R >> >>"
                 f" /Contents {draw_num} 0 R >>"
                 ).encode()
        image = (f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height}"
                 f" /ColorSpace {colour_spaces[components]} /BitsPerComponent 8{decode}"
                 f" /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n"
                 ).encode() + jpeg + b"\nendstream"
        draw  = f"q {page_width} 0 0 {page_height} 0 0 cm /Im0 Do Q".encode()
        draw  = f"<< /Length {len(draw)} >>\nstream\n".encode() + draw + b"\nendstream"

        objects   += [page, image, draw]
        page_refs.append(f"{page_num} 0 R")

    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>".encode()

    # Write out the objects, noting where each starts so the cross-reference
    # table at the end can point to them
    pdf     = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start = 1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_pos  = len(pdf)
    pdf      += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n".encode()
    pdf      += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                 f"startxref\n{xref_pos}\n%%EOF\n"
                 ).encode()
    return bytes(pdf)


def pdf_filename(note_title):
    """
    Make a PDF filename from a note title.

    Characters that can't go in a filename are replaced with underscores.

    ## Parameters

    - `note_title`: Title of the note

    ## Returns

    The filename, ending ".pdf".
    """
    return BAD_FILENAME_CHARS.sub("_", note_title) + ".pdf"


def parse_args():
    """
    Read the command-line arguments.

    ## Returns

    An `argparse.Namespace` with `notebook`, `title`, `output_dir` and `db`
    attributes.
    """
    parser = argparse.ArgumentParser(description = "Make a PDF of the JPEG images in an Evernote note.")
    parser.add_argument("notebook",
                        help = "Name of the notebook",
                        )
    parser.add_argument("title",
                        help = "Title of the note; the PDF is named after it",
                        )
    parser.add_argument("--output-dir",
                        default = ".",
                        help    = "Folder to save the PDF in (default: the current folder)",
                        )
    parser.add_argument("--db",
                        help = "Path to the Evernote RemoteGraph.sql database (found automatically if omitted)",
                        )
    return parser.parse_args()


def main():
    """
    Make the PDF and print where it was saved.
    """
    args = parse_args()

    try:
        db_path    = args.db or list_note_titles.find_database()
        connection = list_note_titles.open_read_only(db_path)
        note_id    = find_note(connection, args.notebook, args.title)
        hashes     = get_jpeg_hashes(connection, note_id)
        if not hashes:
            raise LookupError(f"The note '{args.title}' has no JPEG images")

        content = read_note_content(db_path, note_id)
        if content is None:
            print("Warning: Couldn't find the note's content, so the pages may be out of order",
                  file = sys.stderr,
                  )
        hashes = order_hashes(hashes, content)

        jpegs = []
        for data_hash in hashes:
            path = attachment_path(db_path, note_id, data_hash)
            if not os.path.exists(path):
                raise FileNotFoundError(f"An image in the note hasn't been downloaded ({path})."
                                        f" Open the note in Evernote to download it."
                                        )
            with open(path, "rb") as f:
                data = f.read()

            # Evernote sometimes labels other kinds of image as JPEGs
            if not data.startswith(b"\xff\xd8"):
                raise ValueError(f"An image in the note is labelled as a JPEG but isn't one ({path})")
            jpegs.append(data)

        pdf = make_pdf(jpegs)

        out_path = os.path.join(args.output_dir, pdf_filename(args.title))
        with open(out_path, "wb") as f:
            f.write(pdf)
    except (FileNotFoundError, LookupError, ValueError, OSError, sqlite3.Error) as e:
        print(f"Error: {e}", file = sys.stderr)
        sys.exit(1)

    print(f"Saved {len(jpegs)} page(s) to {out_path}")


if __name__ == "__main__":
    main()
