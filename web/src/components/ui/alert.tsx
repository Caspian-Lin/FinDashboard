import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { AlertCircle, CheckCircle2, Info, AlertTriangle, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

const alertVariants = cva(
  "relative w-full rounded-lg border p-4 [&>svg]:absolute [&>svg]:left-4 [&>svg]:top-4 [&>svg]:text-foreground [&>svg~*]:pl-7",
  {
    variants: {
      variant: {
        default: "border-border bg-card text-card-foreground",
        destructive: "border-destructive/30 bg-destructive/5 text-destructive [&>svg]:text-destructive",
        success: "border-success/30 bg-success/5 text-success [&>svg]:text-success",
        warning: "border-warning/30 bg-warning/5 text-warning [&>svg]:text-warning",
        info: "border-info/30 bg-info/5 text-info [&>svg]:text-info",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

const iconMap = {
  default: AlertCircle,
  destructive: XCircle,
  success: CheckCircle2,
  warning: AlertTriangle,
  info: Info,
};

const Alert = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof alertVariants>
>(({ className, variant = "default", children, ...props }, ref) => {
  const Icon = iconMap[variant || "default"];
  return (
    <div ref={ref} role="alert" className={cn(alertVariants({ variant }), className)} {...props}>
      <Icon className="h-4 w-4" />
      {children}
    </div>
  );
});
Alert.displayName = "Alert";

const AlertTitle = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => (
    <h5 ref={ref} className={cn("mb-1 font-medium leading-none tracking-tight", className)} {...props} />
  ),
);
AlertTitle.displayName = "AlertTitle";

const AlertDescription = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLParagraphElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("text-sm opacity-90 [&_p]:leading-relaxed", className)} {...props} />
  ),
);
AlertDescription.displayName = "AlertDescription";

export { Alert, AlertTitle, AlertDescription };
