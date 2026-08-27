"""The deprecation helper: nothing public disappears without warning first."""

from __future__ import annotations

import warnings

import pytest

from alhazen._deprecation import deprecated, deprecation_message, warn_deprecated_argument


class TestMessages:
    def test_it_names_the_version_and_the_replacement(self):
        # A DeprecationWarning that says only "deprecated" leaves the reader
        # exactly where they started.
        message = deprecation_message("old_thing", "1.1", "1.2", "Task.build_trial")
        assert "1.1" in message and "1.2" in message and "Task.build_trial" in message

    def test_a_replacement_is_optional(self):
        assert "use" not in deprecation_message("old_thing", "1.1", "1.2")


class TestDecorator:
    def test_the_function_still_works(self):
        @deprecated(since="1.1", removed_in="1.2", instead="new_way")
        def old_way(a, b):
            return a + b

        with pytest.warns(DeprecationWarning, match="removed in 1.2"):
            assert old_way(2, 3) == 5

    def test_it_keeps_the_functions_identity(self):
        @deprecated(since="1.1", removed_in="1.2")
        def old_way():
            """Does the old thing."""

        assert old_way.__name__ == "old_way"
        assert "Does the old thing." in old_way.__doc__
        assert "deprecated:: 1.1" in old_way.__doc__

    def test_the_warning_points_at_the_caller(self):
        @deprecated(since="1.1", removed_in="1.2")
        def old_way():
            return 1

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            old_way()
        # This file's line, not the decorator's — the place that has to change.
        assert caught[0].filename == __file__

    def test_an_argument_can_be_deprecated_on_its_own(self):
        def still_supported(old_name=None):
            if old_name is not None:
                warn_deprecated_argument("old_name", "1.1", "1.2", "new_name")
            return old_name

        with pytest.warns(DeprecationWarning, match="old_name"):
            assert still_supported(old_name=3) == 3
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert still_supported() is None  # silent when unused
