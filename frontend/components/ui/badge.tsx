import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const variants = {
  default: "border-brand-100 bg-brand-50 text-brand-700",
  warning: "border-amber-100 bg-amber-50 text-amber-700",
  info: "border-sky-100 bg-sky-50 text-sky-700",
  danger: "border-rose-100 bg-rose-50 text-rose-700",
  neutral: "border-line bg-white text-muted",
};

export function Badge({
  className,
  variant = "default",
  ...props
}: HTMLAttributes<HTMLSpanElement> & {
  variant?: keyof typeof variants;
}) {
  return (
    <span
      className={cn(
        "inline-flex min-h-7 items-center rounded-md border px-2.5 text-xs font-medium",
        variants[variant],
        className,
      )}
      {...props}
    />
  );
}
