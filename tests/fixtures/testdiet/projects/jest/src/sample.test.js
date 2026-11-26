test("passes", () => {
  expect(1 + 1).toBe(2);
});

test("fails", () => {
  expect("sample-failure-message").toBe("a different value");
});

test.skip("skipped", () => {});

test.each([1, 2])("parametrized %i", (value) => {
  expect(value).toBeGreaterThan(0);
});

describe("group", () => {
  test("in group", () => {
    expect(true).toBe(true);
  });

  describe("nested", () => {
    test("in nested group", () => {
      expect(true).toBe(true);
    });
  });
});

test("sleeps_half_a_second", async () => {
  await new Promise((resolve) => setTimeout(resolve, 500));
});
