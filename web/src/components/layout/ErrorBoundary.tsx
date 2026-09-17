import * as React from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

interface ErrorBoundaryState {
  hasError: boolean;
  error?: Error;
}

/** 兜底 UI 抽成函数组件:ErrorBoundary 自身是 class 组件,不能调用 useT。 */
function ErrorFallback({ error, onReset }: { error?: Error; onReset: () => void }) {
  const { t } = useT();
  return (
    <div className="flex min-h-[400px] flex-col items-center justify-center p-8 text-center">
      <AlertTriangle className="mb-4 h-12 w-12 text-warning" />
      <h2 className="text-lg font-semibold text-foreground">{t("errorBoundary.title")}</h2>
      <p className="mt-1 max-w-md text-sm text-muted-foreground">
        {error?.message ?? t("errorBoundary.unknown")}
      </p>
      <Button onClick={onReset} variant="outline" size="sm" className="mt-4">
        <RefreshCw className="mr-2 h-4 w-4" />
        {t("common.retry")}
      </Button>
    </div>
  );
}

export class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  handleReset = () => {
    this.setState({ hasError: false, error: undefined });
  };

  render() {
    if (this.state.hasError) {
      return (
        <ErrorFallback error={this.state.error} onReset={this.handleReset} />
      );
    }
    return this.props.children;
  }
}
