import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MasterList } from "./master-list";

describe("MasterList", () => {
  it("allows a long action group to wrap below the title instead of overflowing", () => {
    render(
      <MasterList
        title="Experiment list"
        count={11}
        actions={<button type="button">Create validation experiment</button>}
      >
        <div>Experiment</div>
      </MasterList>,
    );

    const action = screen.getByRole("button", {
      name: "Create validation experiment",
    });
    const actionGroup = action.parentElement;
    const headerRow = actionGroup?.parentElement;

    expect(headerRow).toHaveClass("flex-wrap");
    expect(actionGroup).toHaveClass("max-w-full", "flex-wrap");
  });
});
