import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import App from "./App";

afterEach(() => {
  cleanup();
});

test("renders default production panels", () => {
  render(<App />);
  expect(screen.getByText("Live Search")).toBeTruthy();
  expect(screen.getByText("Search Intelligence")).toBeTruthy();
  expect(screen.getByText("Proactive Actions")).toBeTruthy();
  expect(screen.getByText("Output Preview")).toBeTruthy();
  expect(screen.getByText("Skygrep Agent")).toBeTruthy();
});

test("renders visual mode toggle", () => {
  render(<App />);
  expect(screen.getByRole("tablist", { name: "Visual mode" })).toBeTruthy();
  expect(screen.getByText("3D")).toBeTruthy();
});

test("result cards drive output preview state", () => {
  render(<App />);
  fireEvent.click(screen.getByText("benchmarks/parity_vs_ripgrep.py"));
  expect(screen.getByRole("heading", { name: "parity_vs_ripgrep.py" })).toBeTruthy();
});

test("suggested actions mutate the output preview", () => {
  render(<App />);
  fireEvent.click(screen.getAllByText("Trace token-savings benchmark gap")[1]);
  expect(screen.getByRole("heading", { name: "Proactive Search Candidate" })).toBeTruthy();
});

test("command quick actions expose router and attachment state", () => {
  render(<App />);
  fireEvent.click(screen.getAllByRole("button", { name: "Explain route" })[0]);
  expect(screen.getByRole("heading", { name: "SkyGrab Router Trace" })).toBeTruthy();
});

test("command route preview automatically chooses skygrep options", () => {
  render(<App />);
  expect(screen.getAllByText("skygrep --json --content --detail standard").length).toBeGreaterThan(0);
  fireEvent.change(screen.getByLabelText("Skygrep command"), {
    target: { value: "where is token refresh implemented?" },
  });
  expect(screen.getAllByText("skygrep --json --top 10").length).toBeGreaterThan(0);
});

test("detail quick action scopes to the selected evidence path", () => {
  render(<App />);
  fireEvent.click(screen.getAllByRole("button", { name: "Need detail" })[0]);
  expect(screen.getByRole("heading", { name: "Detail Request" })).toBeTruthy();
  expect(screen.getByText(/skygrep --content --detail full/)).toBeTruthy();
});

test("live search filters are clickable and meaningful", () => {
  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Docs" }));
  expect(screen.getByText("Waiting for ranked local evidence...")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  expect(screen.getByText("benchmarks/token_savings.py")).toBeTruthy();
});

test("bottom dock is functional navigation state", () => {
  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: /Settings/i }));
  expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy();
});

test("workflow graph lives behind the workflows dock", () => {
  render(<App />);
  expect(screen.queryByText("Workflow Graph")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Workflows/i }));
  expect(screen.getByText("Workflow Graph")).toBeTruthy();
});
