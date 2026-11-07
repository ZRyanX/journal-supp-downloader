import io
import zipfile

def office_archive(payload="data"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in ["[Content_Types].xml", "xl/workbook.xml", "word/document.xml", "ppt/presentation.xml"]:
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            archive.writestr(info, payload)
    return stream.getvalue()
