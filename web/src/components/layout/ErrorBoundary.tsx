import * as React from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ErrorBoundaryState {
  hasError: boolean;
  error?: Error;
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
        <div className="flex min-h-[400px] flex-col items-center justify-center p-8 text-center">
          <AlertTriangle className="mb-4 h-12 w-12 text-warning" />
          <h2 className="text-lg font-semibold text-foreground">页面出错了</h2>
          <p className="mt-1 max-w-md text-sm text-muted-foreground">
            {this.state.error?.message ?? "发生了未知错误"}
          </p>
          <Button onClick={this.handleReset} variant="outline" size="sm" className="mt-4">
            <RefreshCw className="mr-2 h-4 w-4" />
            重试
          </Button>
        </div>
      );
    }
    return this.props.children;
  }
}
