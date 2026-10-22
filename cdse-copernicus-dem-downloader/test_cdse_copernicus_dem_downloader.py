"""Run with: python -m unittest test_cdse_copernicus_dem_downloader"""

import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from zipfile import ZipFile

import requests

import cdse_copernicus_dem_downloader as m

WKT = "MULTIPOLYGON(((1 1,2 1,2 2,1 2,1 1)))"
DESC = f'LL_WKT <font COLOR="#008000">{WKT}</font>'
KML = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder>
<Placemark><name>31TCJ</name><description><![CDATA[{DESC}]]></description></Placemark>
</Folder></Document></kml>"""


def placemark(description):
    return SimpleNamespace(description=SimpleNamespace(text=description))


class DemDownloaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = m.DemDownloader()
        self.d.dem_directory = os.path.join(self.tmp.name, "out")
        self.d.aux_directory = self.tmp.name

    def mock_response(self, status=200, chunks=(b"abc",), fail=False):
        resp = mock.MagicMock(status_code=status)
        if fail:
            resp.iter_content.side_effect = requests.ConnectionError("boom")
        else:
            resp.iter_content.return_value = chunks
        self.d.session = mock.MagicMock()
        self.d.session.get.return_value.__enter__.return_value = resp
        return resp

    # --- KML / multipolygon
    def test_load_kml_and_retrieve_multipolygon(self):
        path = os.path.join(self.tmp.name, "t.kml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(KML)
        self.d.load_kml(path)
        self.assertEqual(self.d.retrieve_multipolygon("31TCJ"), "1 1,2 1,2 2,1 2,1 1")
        self.assertFalse(self.d.antimeridian_status)

    def test_retrieve_multipolygon_unknown_tile(self):
        self.assertFalse(self.d.retrieve_multipolygon("NOPE"))

    def test_retrieve_multipolygon_malformed_description(self):
        self.d.kml_placemarks = {"T": placemark("no geometry here")}
        self.assertFalse(self.d.retrieve_multipolygon("T"))

    # --- catalogue
    def test_retrieve_dem_list_caches(self):
        self.d.session = mock.MagicMock()
        self.d.session.get.return_value.json.return_value = {"value": [{"Id": "a"}, {"Id": "b"}]}
        self.assertEqual(self.d.retrieve_dem_list("u"), ["a", "b"])
        self.assertEqual(self.d.retrieve_dem_list("u"), ["a", "b"])
        self.assertEqual(self.d.session.get.call_count, 1)

    def test_retrieve_dem_list_raises_on_error(self):
        self.d.session = mock.MagicMock()
        self.d.session.get.side_effect = requests.ConnectionError("down")
        with self.assertRaises(m.DemDownloaderException):
            self.d.retrieve_dem_list("u")
        self.d.session.get.side_effect = None
        self.d.session.get.return_value.json.return_value = {"unexpected": 1}
        with self.assertRaises(m.DemDownloaderException):
            self.d.retrieve_dem_list("u2")

    # --- download
    def test_download_failure_leaves_no_partial_file(self):
        self.mock_response(fail=True)
        with self.assertRaises(requests.ConnectionError):
            self.d.downloading_dem("id1", "tok")
        self.assertEqual(os.listdir(self.d.dem_directory), [])

    def test_download_403_raises(self):
        resp = self.mock_response(status=403)
        resp.json.return_value = {"code": "DAT-ZIP-608"}
        with self.assertRaises(m.DemDownloaderException):
            self.d.downloading_dem("id1", "tok")

    def test_download_extracts_and_removes_zip(self):
        buf = io.BytesIO()
        with ZipFile(buf, "w") as z:
            z.writestr("x/Copernicus_DSM_90_N00_00_E000_00_DEM.tif", b"data")
        self.mock_response(chunks=(buf.getvalue(),))
        self.d.downloading_dem("id1", "tok")
        self.assertEqual(os.listdir(self.d.dem_directory), ["Copernicus_DSM_90_N00_00_E000_00_DEM.tif"])
        self.assertEqual(self.d.dict_filename_dem_id["Copernicus_DSM_90_N00_00_E000_00_DEM.tif"], "id1")
        self.assertIn("id1", self.d.processed_dem_id_file)

    def test_bad_zip_is_removed(self):
        self.mock_response(chunks=(b"not a zip",))
        self.d.downloading_dem("id1", "tok")  # extraction error is logged, not raised
        self.assertEqual(os.listdir(self.d.dem_directory), [])

    # --- dict
    def test_save_dict(self):
        self.d.dict_filename_dem_id = {"f.tif": "id"}
        self.d.save_dict()
        with open(os.path.join(self.tmp.name, self.d.dict_filename_dem_id_file), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"f.tif": "id"})


if __name__ == "__main__":
    unittest.main()
