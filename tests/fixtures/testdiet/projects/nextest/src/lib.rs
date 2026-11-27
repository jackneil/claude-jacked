//! A small crate for the test-diet sample reports.

pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[cfg(test)]
mod tests {
    use super::add;

    #[test]
    fn passes() {
        assert_eq!(add(1, 1), 2);
    }

    #[test]
    fn fails() {
        assert_eq!("sample-failure-message", "a different value");
    }

    #[test]
    #[ignore = "sample-skip-reason"]
    fn skipped() {}

    #[test]
    fn table() {
        for (input, expected) in [(1, 1), (2, 2)] {
            assert_eq!(add(input, 0), expected);
        }
    }

    mod group {
        #[test]
        fn in_nested_group() {}
    }

    #[test]
    fn sleeps_half_a_second() {
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
}
