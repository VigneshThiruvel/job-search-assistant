import { notifications } from "@mantine/notifications";
import { useMutation, useQueryClient } from "@tanstack/react-query";

// A write, a toast, then a refetch of everything it can change (lists, detail, counts).
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>, success?: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: A) => fn(...args),
    onSuccess: () => {
      if (success) notifications.show({ color: "teal", message: success });
      client.invalidateQueries();
    },
    onError: (e: Error) => notifications.show({ color: "red", title: "Not done", message: e.message }),
  });
}
