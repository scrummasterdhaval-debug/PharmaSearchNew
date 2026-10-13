"""Offline connector checks using temporary workbooks and a real SQLite index."""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import openpyxl
import requests

from sources import nmpa_china as nmpa, open_registers
from sources.source_registry import CONNECTORS


class NmpaChinaTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for target, value in (
            ("sources.open_registers.INDEX_DIR", self.root),
            ("sources.nmpa_china._CACHE_PATH", self.root / "names.json"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch("requests.get", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)

    def fixture_archive(self):
        path = self.root / "drugs.zip"
        common = ["批准文号", "药品通用名称", "剂型", "规格", "药品编码"]
        sheets = [
            ("domestic.xlsx", common + ["上市许可持有人", "生产单位"], [
                ["H001", "盐酸二甲双胍", "片剂", "500mg", "00123", "Holder", "Maker"],
                ["H002", "布洛芬", "片剂", "200mg", "00124", "Other", "Other maker"],
                [None, "无批准文号", "片剂", "10mg", "00125", "Other", "Maker"],
            ]),
            ("imported.xlsx", common + ["上市许可持有人英文", "上市许可持有人中文"], [
                ["J001", "盐酸二甲双胍", "片剂", "850mg", "00126", "Import Holder", "进口持有人"],
            ]),
        ]
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("readme.txt", "Not a workbook")
            for name, headers, rows in sheets:
                workbook = openpyxl.Workbook()
                workbook.active.append(["National Drug Code list"])
                workbook.active.append(headers)
                for row in rows:
                    workbook.active.append(row)
                output = io.BytesIO()
                workbook.save(output)
                workbook.close()
                archive.writestr(name, output.getvalue())
        return path

    def test_workbooks_index_and_english_search_preserve_source_values(self):
        open_registers.build_index(nmpa.NMPA_CHINA, self.fixture_archive())
        rows = nmpa.run_nmpa_china_search("metformin")
        self.assertEqual(len(rows), 2)
        domestic, imported = sorted(rows, key=lambda row: row["registration_number"])
        self.assertEqual(domestic["drug_code"], "00123")
        self.assertEqual(domestic["manufacturer_name"], "Maker")
        self.assertEqual(domestic["company"], "Holder")
        self.assertEqual(domestic["product"], "盐酸二甲双胍 500mg")
        self.assertEqual(domestic["dosage_form"], "Tablet")
        self.assertEqual(domestic["source_substance"], "盐酸二甲双胍")
        self.assertEqual(domestic["searched_substance"], "metformin")
        self.assertEqual(imported["company"], "Import Holder")
        self.assertEqual(imported["classification"], "Imported")
        self.assertEqual(imported["manufacturer_name"], "")
        self.assertEqual(imported["manufacturer_country"], "")
        self.assertEqual(len(nmpa.run_nmpa_china_search("二甲双胍")), 2)

    def test_missing_index_requests_refresh_and_reports_not_ready(self):
        for started in (True, False):
            with self.subTest(started=started), patch.object(nmpa, "refresh_in_background", return_value=started) as refresh:
                with self.assertRaises(open_registers.RegisterNotReady):
                    nmpa.run_nmpa_china_search("metformin")
                refresh.assert_called_once_with(nmpa.NMPA_CHINA)

    def test_stale_index_remains_searchable_while_refreshing(self):
        path = open_registers.build_index(nmpa.NMPA_CHINA, self.fixture_archive())
        with open_registers._connect(path) as connection:
            connection.execute("UPDATE meta SET value = '0' WHERE key = 'fetched_epoch'")
        with patch.object(nmpa, "refresh_in_background") as refresh:
            self.assertEqual(len(nmpa.run_nmpa_china_search("metformin")), 2)
            refresh.assert_called_once_with(nmpa.NMPA_CHINA)

    def test_result_limit_reports_total_matches(self):
        open_registers.build_index(nmpa.NMPA_CHINA, self.fixture_archive())
        with patch.object(nmpa, "MAX_RESULTS", 1):
            rows = nmpa.run_nmpa_china_search("metformin")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["available_total"], 2)

    def test_known_aliases_need_no_translation_and_are_deduplicated(self):
        self.assertEqual(nmpa.chinese_names(" Acetaminophen "), ["对乙酰氨基酚"])

    def test_translation_is_cached(self):
        with patch.object(nmpa, "_wikidata_chinese_name", return_value="测试药") as lookup:
            self.assertEqual(nmpa.chinese_names("testdrug"), ["测试药"])
            self.assertEqual(nmpa.chinese_names("testdrug"), ["测试药"])
            lookup.assert_called_once_with("testdrug")

    def test_translation_failure_does_not_start_register_download(self):
        with patch.object(nmpa, "_wikidata_chinese_name", side_effect=requests.Timeout), patch.object(nmpa, "refresh_in_background") as refresh:
            self.assertEqual(nmpa.run_nmpa_china_search("testdrug"), [])
            refresh.assert_not_called()
        self.assertFalse((self.root / "names.json").exists())

    def test_fetch_resolves_relative_release_and_archive_links(self):
        listing = Mock(content='<a href="/release.html">国家药品编码本位码（截至2026年）</a>'.encode())
        release = Mock(content=b'<a href="/files/drugs.zip">Download</a>')
        with patch.object(nmpa.requests, "get", side_effect=[listing, release]) as get, patch.object(nmpa, "download_file") as download:
            result = nmpa.fetch_nmpa(nmpa.NMPA_CHINA, self.root)
        self.assertIs(result, download.return_value)
        self.assertEqual(get.call_args_list[1].args[0], "https://www.nmpa.gov.cn/release.html")
        download.assert_called_once_with(nmpa.NMPA_CHINA, "https://www.nmpa.gov.cn/files/drugs.zip", self.root / "nmpa_drug_codes.zip")
        listing.raise_for_status.assert_called_once()
        release.raise_for_status.assert_called_once()

    def test_missing_release_is_reported(self):
        with patch.object(nmpa.requests, "get", return_value=Mock(content=b"<html></html>")):
            with self.assertRaisesRegex(RuntimeError, "lists no release"):
                nmpa.fetch_nmpa(nmpa.NMPA_CHINA, self.root)

    def test_registry_uses_new_connector(self):
        connector, = [item for item in CONNECTORS if item.metadata.name == "NMPA China"]
        with patch.object(nmpa, "chinese_names", return_value=[]) as names:
            self.assertEqual(connector.search("testdrug"), [])
            names.assert_called_once_with("testdrug")

    def test_dosage_forms_are_translated_only_where_known(self):
        self.assertEqual(nmpa.english_dosage_form("片剂"), "Tablet")
        self.assertEqual(nmpa.english_dosage_form("片剂（薄膜衣）"), "Tablet (film-coated)")
        self.assertEqual(nmpa.english_dosage_form("注射剂(冻干)"), "Injection (lyophilised)")
        self.assertEqual(nmpa.english_dosage_form("片剂（未知）"), "Tablet (未知)")
        self.assertEqual(nmpa.english_dosage_form("未知剂"), "未知剂")


if __name__ == "__main__":
    unittest.main()
