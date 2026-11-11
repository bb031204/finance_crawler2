import unittest
from numeric_qualifiers import validate_numeric_qualifiers


class NumericQualifierTests(unittest.TestCase):
    def test_approximation_cannot_become_lower_bound(self):
        with self.assertRaises(ValueError):validate_numeric_qualifiers('The company has over 650 employees.','It had around 650 employees.')
    def test_approximation_synonyms_are_preserved(self):
        validate_numeric_qualifiers('It had approximately 650 employees.','It had around 650 employees.')
    def test_lower_bound_cannot_become_exact(self):
        with self.assertRaises(ValueError):validate_numeric_qualifiers('Revenue grew 50%.','Revenue grew over 50 percent.')
    def test_upper_and_lower_bound_cannot_be_swapped(self):
        with self.assertRaises(ValueError):validate_numeric_qualifiers('Revenue grew at least 15%.','Revenue grew less than 15%.')
    def test_different_units_are_not_conflated(self):
        validate_numeric_qualifiers('There are 50 employees.','Revenue grew over 50%.')
    def test_ambiguous_repeated_values_are_not_guessed(self):
        validate_numeric_qualifiers('Growth was 50%.','One segment grew over 50%. Another segment grew 50%.')
    def test_chinese_bounds_are_checked(self):
        with self.assertRaises(ValueError):validate_numeric_qualifiers('该公司有超过650员工。','该公司有约650员工。')
    def test_inclusive_bound_cannot_become_strict(self):
        with self.assertRaises(ValueError):validate_numeric_qualifiers('Growth was over 50%.','Growth was at least 50%.')
        with self.assertRaises(ValueError):validate_numeric_qualifiers('Growth was under 50%.','Growth was up to 50%.')
    def test_chinese_employee_classifier_is_supported(self):
        with self.assertRaises(ValueError):validate_numeric_qualifiers('公司有超过650名员工。','公司有约650名员工。')
    def test_word_suffix_does_not_become_a_bound(self):
        validate_numeric_qualifiers('The target is 50%.','Turnover 50%.')


if __name__=='__main__':unittest.main()
