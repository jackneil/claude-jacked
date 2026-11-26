// These tests use no page fixture, so Playwright opens no browser.
const { test, expect } = require("@playwright/test");

test("fails", async () => {
  expect("sample-failure-message").toBe("a different value");
});

test.skip("skipped", async () => {});

for (const value of [1, 2]) {
  test(`parametrized ${value}`, async () => {
    expect(value).toBeGreaterThan(0);
  });
}

test.describe("group", () => {
  test("in group", async () => {
    expect(true).toBe(true);
  });

  test.describe("nested", () => {
    test("in nested group", async () => {
      expect(true).toBe(true);
    });
  });
});

test("sleeps_half_a_second", async () => {
  await new Promise((resolve) => setTimeout(resolve, 500));
});

test("passes", async () => {
  expect(1 + 1).toBe(2);
});
