#!/usr/bin/env python

import hashlib
import os
import re
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from dateutil.relativedelta import relativedelta
from deid.data import get_dataset
from deid.dicom import get_files
from deid.dicom.parser import DicomParser
from deid.tests.common import create_recipe, get_file, get_same_file
from deid.utils import get_installdir
from pydicom.dataset import Dataset

uuid_regex = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


class TestDicomFuncs(unittest.TestCase):
    def setUp(self):
        self.pwd = get_installdir()
        self.dataset = None
        self.dataset_error = None
        try:
            self.dataset = get_dataset("humans")
        except ValueError as exc:
            self.dataset_error = exc
        self.tmpdir = tempfile.mkdtemp()
        print("\n######################START######################")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        print("\n######################END########################")

    def _require_dataset(self):
        if self.dataset is None:
            self.skipTest(str(self.dataset_error or "deid-data not available"))

    @staticmethod
    def _make_dataset(
        patient_id="TESTPATIENT",
        study_date=None,
        birth_date=None,
        acquisition_date=None,
    ):
        dataset = Dataset()
        dataset.PatientID = patient_id
        if study_date is not None:
            dataset.StudyDate = study_date
        if birth_date is not None:
            dataset.PatientBirthDate = birth_date
        if acquisition_date is not None:
            dataset.AcquisitionDate = acquisition_date
        return dataset

    @staticmethod
    def _parse_date(value):
        return datetime.strptime(str(value), "%Y%m%d").date()

    @staticmethod
    def _expected_day_offset(patient_id, salt):
        hash_source = f"{salt}|{patient_id}".encode("utf-8")
        hash_int = int(hashlib.sha256(hash_source).hexdigest(), 16)
        day_offset = (hash_int % 365) + 1
        if hash_int & 1:
            day_offset = -day_offset
        return day_offset

    @classmethod
    def _expected_jittered_date(cls, original_date, patient_id, salt):
        original = cls._parse_date(original_date)
        offset = cls._expected_day_offset(patient_id, salt)
        return (original + timedelta(days=offset)).strftime("%Y%m%d")

    def test_user_provided_func(self):
        """
        %header
        REMOVE ALL func:myfunction
        """
        print("Test user provided func")
        self._require_dataset()
        dicom_file = next(get_files(self.dataset, pattern="ctbrain1.dcm"))

        def myfunction(dicom, value, field, item):
            from pydicom.tag import Tag

            tag = Tag(field.element.tag)

            if tag in dicom:
                currentvalue = str(dicom.get(tag).value).lower()
                if "hibbard" in currentvalue:
                    return True
                return False

        actions = [{"action": "REMOVE", "field": "ALL", "value": "func:myfunction"}]
        recipe = create_recipe(actions)

        # Create a parser, define function for it
        parser = DicomParser(dicom_file, recipe=recipe)
        parser.define("myfunction", myfunction)
        parser.parse()

        self.assertEqual(174, len(parser.dicom))
        with self.assertRaises(KeyError):
            parser.dicom["ReferringPhysicianName"].value
        with self.assertRaises(KeyError):
            parser.dicom["PhysiciansOfRecord"].value
        with self.assertRaises(KeyError):
            parser.dicom["RequestingPhysician"].value
        with self.assertRaises(KeyError):
            parser.dicom["00331019"].value

    def test_basic_uuid(self):
        """
        %header
        REPLACE ReferringPhysicianName deid_func:basic_uuid
        """
        print("Test deid_func:basic_uuid")

        self._require_dataset()
        dicom_file = get_file(self.dataset)
        actions = [
            {
                "action": "REPLACE",
                "field": "ReferringPhysicianName",
                "value": "deid_func:basic_uuid",
            }
        ]
        recipe = create_recipe(actions)

        # Create a parser, define function for it
        parser = DicomParser(dicom_file, recipe=recipe)
        parser.parse()

        # 8905e722-8103-4823-bc8f-8aed967e272d
        print(parser.dicom["ReferringPhysicianName"].value)
        assert re.search(uuid_regex, str(parser.dicom["ReferringPhysicianName"].value))

    def test_pydicom_uuid(self):
        """
        %header
        REPLACE ReferringPhysicianName deid_func:pydicom_uuid
        """
        print("Test deid_func:pydicom_uuid")

        self._require_dataset()
        dicom_file = get_file(self.dataset)
        actions = [
            {
                "action": "REPLACE",
                "field": "ReferringPhysicianName",
                "value": "deid_func:pydicom_uuid",
            }
        ]
        recipe = create_recipe(actions)

        # Create a parser, define function for it
        parser = DicomParser(dicom_file, recipe=recipe)
        parser.parse()

        # Randomness is anything, but should be all numbers
        print(parser.dicom["ReferringPhysicianName"].value)
        name = str(parser.dicom["ReferringPhysicianName"].value)
        assert re.search("([0-9]|.)+", name)

        # This is the pydicom default, and we default to stable remapping
        assert (
            name == "2.25.39101090714049289438893821151950032074223798085258118413707"
        )

        # Add a custom prefix
        # must match '^(0|[1-9][0-9]*)(\\.(0|[1-9][0-9]*))*\\.$'
        actions = [
            {
                "action": "REPLACE",
                "field": "ReferringPhysicianName",
                "value": "deid_func:pydicom_uuid prefix=1.55.",
            }
        ]
        recipe = create_recipe(actions)
        parser = DicomParser(dicom_file, recipe=recipe)
        parser.parse()

        # Randomness is anything, but should be all numbers
        print(parser.dicom["ReferringPhysicianName"].value)
        name = str(parser.dicom["ReferringPhysicianName"].value)
        assert name.startswith("1.55.")

        # This should always be consistent if we use the original as entropy
        dicom_file = get_same_file(self.dataset)
        actions = [
            {
                "action": "REPLACE",
                "field": "ReferringPhysicianName",
                "value": "deid_func:pydicom_uuid stable_remapping=false",
            }
        ]
        recipe = create_recipe(actions)
        parser = DicomParser(dicom_file, recipe=recipe)
        parser.parse()

        # Randomness is anything, but should be all numbers
        print(parser.dicom["ReferringPhysicianName"].value)
        name = str(parser.dicom["ReferringPhysicianName"].value)
        assert (
            name != "2.25.39101090714049289438893821151950032074223798085258118413707"
        )

    def test_suffix_uuid(self):
        """
        %header
        REPLACE ReferringPhysicianName deid_func:suffix_uuid
        """
        print("Test deid_func:basic_uuid")

        self._require_dataset()
        dicom_file = get_file(self.dataset)
        actions = [
            {
                "action": "REPLACE",
                "field": "ReferringPhysicianName",
                "value": "deid_func:suffix_uuid",
            }
        ]
        recipe = create_recipe(actions)

        # Create a parser, define function for it
        parser = DicomParser(dicom_file, recipe=recipe)
        parser.parse()

        # 8905e722-8103-4823-bc8f-8aed967e272d
        print(parser.dicom["ReferringPhysicianName"].value)
        name = str(parser.dicom["ReferringPhysicianName"].value)
        assert "referringphysicianname-" in name
        assert re.search(uuid_regex, name)

    def test_dicom_uuid(self):
        """
        %header
        REPLACE ReferringPhysicianName deid_func:suffix_uuid org=myorg
        """
        print("Test deid_func:dicom_uuid")

        self._require_dataset()
        dicom_file = get_file(self.dataset)
        actions = [
            {
                "action": "REPLACE",
                "field": "ReferringPhysicianName",
                "value": "deid_func:dicom_uuid org_root=1.2.826.0.1.3680043.10.188",
            }
        ]
        recipe = create_recipe(actions)

        # Create a parser, define function for it
        parser = DicomParser(dicom_file, recipe=recipe)
        parser.parse()

        # 8905e722-8103-4823-bc8f-8aed967e272d
        print(parser.dicom["ReferringPhysicianName"].value)
        name = str(parser.dicom["ReferringPhysicianName"].value)
        assert "1.2.826.0.1.3680043.10.188" in name
        assert len(name) == 64

    def test_dicom_jitter(self):
        """RECIPE RULE
        REPLACE AcquisitionDate deid_func:jitter
        """
        print("Test deid_func:jitter")

        patient_id = "JITTER-PATIENT"
        salt = "unit-test-salt"
        dataset = self._make_dataset(
            patient_id=patient_id,
            acquisition_date="20230101",
        )
        actions = [
            {
                "action": "REPLACE",
                "field": "AcquisitionDate",
                "value": "deid_func:jitter",
            }
        ]
        recipe = create_recipe(actions)

        parser = DicomParser(dataset, recipe=recipe)

        original_date = parser.dicom.AcquisitionDate
        assert original_date == "20230101"
        with mock.patch.dict(os.environ, {"SECRET_SALT": salt}, clear=False):
            parser.parse()
        jittered_date = str(parser.dicom["AcquisitionDate"].value)
        expected = self._expected_jittered_date("20230101", patient_id, salt)
        assert jittered_date == expected

    def test_birthdate_cap_preserves_under_90_interval(self):
        print("Test deid_func:jitter_birthdate_cap_89 under age 90")

        patient_id = "UNDER90-PATIENT"
        salt = "unit-test-salt"
        original_study = "20250301"
        original_birth = "19850301"
        dataset = self._make_dataset(
            patient_id=patient_id,
            study_date=original_study,
            birth_date=original_birth,
        )
        recipe = create_recipe(
            [
                {
                    "action": "REPLACE",
                    "field": "StudyDate",
                    "value": "deid_func:jitter",
                },
                {
                    "action": "REPLACE",
                    "field": "PatientBirthDate",
                    "value": "deid_func:jitter_birthdate_cap_89",
                },
            ]
        )

        parser = DicomParser(dataset, recipe=recipe)
        with mock.patch.dict(os.environ, {"SECRET_SALT": salt}, clear=False):
            parser.parse()

        original_delta = self._parse_date(original_study) - self._parse_date(
            original_birth
        )
        deidentified_delta = self._parse_date(parser.dicom.StudyDate) - self._parse_date(
            parser.dicom.PatientBirthDate
        )
        expected_birth = self._expected_jittered_date(original_birth, patient_id, salt)

        self.assertEqual(original_delta.days, deidentified_delta.days)
        self.assertEqual(expected_birth, str(parser.dicom.PatientBirthDate))

    def test_birthdate_cap_limits_age_to_89_years(self):
        print("Test deid_func:jitter_birthdate_cap_89 over age 89")

        patient_id = "OVER89-PATIENT"
        salt = "unit-test-salt"
        original_study = "20250115"
        original_birth = "19300101"
        dataset = self._make_dataset(
            patient_id=patient_id,
            study_date=original_study,
            birth_date=original_birth,
        )
        recipe = create_recipe(
            [
                {
                    "action": "REPLACE",
                    "field": "StudyDate",
                    "value": "deid_func:jitter",
                },
                {
                    "action": "REPLACE",
                    "field": "PatientBirthDate",
                    "value": "deid_func:jitter_birthdate_cap_89",
                },
            ]
        )

        parser = DicomParser(dataset, recipe=recipe)
        with mock.patch.dict(os.environ, {"SECRET_SALT": salt}, clear=False):
            parser.parse()

        deidentified_study = self._expected_jittered_date(original_study, patient_id, salt)
        jittered_birth = self._expected_jittered_date(original_birth, patient_id, salt)
        expected_cap = (
            self._parse_date(deidentified_study) - relativedelta(years=89)
        ).strftime("%Y%m%d")

        self.assertEqual(deidentified_study, str(parser.dicom.StudyDate))
        self.assertLess(self._parse_date(jittered_birth), self._parse_date(expected_cap))
        self.assertEqual(expected_cap, str(parser.dicom.PatientBirthDate))

    def test_birthdate_cap_leap_day_normalizes_with_relativedelta(self):
        print("Test deid_func:jitter_birthdate_cap_89 leap-day cap")

        patient_id = "LEAPDAY-PATIENT"
        salt = "unit-test-salt"
        target_deidentified_study = date(2024, 2, 29)
        offset = self._expected_day_offset(patient_id, salt)
        original_study = (target_deidentified_study - timedelta(days=offset)).strftime(
            "%Y%m%d"
        )
        dataset = self._make_dataset(
            patient_id=patient_id,
            study_date=original_study,
            birth_date="19000101",
        )
        recipe = create_recipe(
            [
                {
                    "action": "REPLACE",
                    "field": "StudyDate",
                    "value": "deid_func:jitter",
                },
                {
                    "action": "REPLACE",
                    "field": "PatientBirthDate",
                    "value": "deid_func:jitter_birthdate_cap_89",
                },
            ]
        )

        parser = DicomParser(dataset, recipe=recipe)
        with mock.patch.dict(os.environ, {"SECRET_SALT": salt}, clear=False):
            parser.parse()

        expected_cap = (
            target_deidentified_study - relativedelta(years=89)
        ).strftime("%Y%m%d")
        self.assertEqual("20240229", str(parser.dicom.StudyDate))
        self.assertEqual("19350228", expected_cap)
        self.assertEqual(expected_cap, str(parser.dicom.PatientBirthDate))

    def test_birthdate_cap_falls_back_to_plain_jitter_without_study_date(self):
        print("Test deid_func:jitter_birthdate_cap_89 fallback without usable StudyDate")

        patient_id = "FALLBACK-PATIENT"
        salt = "unit-test-salt"

        for study_date in [None, "NOTADATE"]:
            with self.subTest(study_date=study_date):
                capped_dataset = self._make_dataset(
                    patient_id=patient_id,
                    study_date=study_date,
                    birth_date="19300101",
                )
                jitter_dataset = self._make_dataset(
                    patient_id=patient_id,
                    study_date=study_date,
                    birth_date="19300101",
                )
                capped_recipe = create_recipe(
                    [
                        {
                            "action": "REPLACE",
                            "field": "PatientBirthDate",
                            "value": "deid_func:jitter_birthdate_cap_89",
                        }
                    ]
                )
                jitter_recipe = create_recipe(
                    [
                        {
                            "action": "REPLACE",
                            "field": "PatientBirthDate",
                            "value": "deid_func:jitter",
                        }
                    ]
                )

                capped_parser = DicomParser(capped_dataset, recipe=capped_recipe)
                jitter_parser = DicomParser(jitter_dataset, recipe=jitter_recipe)
                with mock.patch.dict(os.environ, {"SECRET_SALT": salt}, clear=False):
                    capped_parser.parse()
                    jitter_parser.parse()

                self.assertEqual(
                    str(jitter_parser.dicom.PatientBirthDate),
                    str(capped_parser.dicom.PatientBirthDate),
                )

    def test_birthdate_cap_uses_deidentified_study_date_from_recipe_order(self):
        print("Test deid_func:jitter_birthdate_cap_89 uses deidentified StudyDate")

        patient_id = "ORDER-PATIENT"
        salt = "unit-test-salt"
        original_study = "20240115"
        dataset = self._make_dataset(
            patient_id=patient_id,
            study_date=original_study,
            birth_date="19200101",
        )
        recipe = create_recipe(
            [
                {
                    "action": "REPLACE",
                    "field": "StudyDate",
                    "value": "deid_func:jitter",
                },
                {
                    "action": "REPLACE",
                    "field": "PatientBirthDate",
                    "value": "deid_func:jitter_birthdate_cap_89",
                },
            ]
        )

        parser = DicomParser(dataset, recipe=recipe)
        with mock.patch.dict(os.environ, {"SECRET_SALT": salt}, clear=False):
            parser.parse()

        deidentified_study = self._parse_date(parser.dicom.StudyDate)
        final_birth = str(parser.dicom.PatientBirthDate)
        expected_from_deidentified_study = (
            deidentified_study - relativedelta(years=89)
        ).strftime("%Y%m%d")
        expected_from_original_study = (
            self._parse_date(original_study) - relativedelta(years=89)
        ).strftime("%Y%m%d")

        self.assertEqual(expected_from_deidentified_study, final_birth)
        self.assertNotEqual(expected_from_original_study, final_birth)


if __name__ == "__main__":
    unittest.main()
