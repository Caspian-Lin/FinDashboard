import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  DEFAULT_FACTOR_SELECTION,
  type FactorSelectionInput,
} from "../lib/api";
import FactorSelectionForm from "./FactorSelectionForm";

describe("FactorSelectionForm", () => {
  it("默认关闭并说明沿用静态标的", () => {
    render(
      <FactorSelectionForm
        value={{ ...DEFAULT_FACTOR_SELECTION }}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText(/旧回测行为保持不变/)).toBeInTheDocument();
    expect(screen.queryByLabelText("排序因子")).not.toBeInTheDocument();
  });

  it("启用后提交明确的候选池开关", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn<(value: FactorSelectionInput) => void>();
    render(
      <FactorSelectionForm
        value={{ ...DEFAULT_FACTOR_SELECTION }}
        onChange={onChange}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: "启用按日选股" }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: true }),
    );
  });
});
