const paths: Record<string, string> = {
  write: "M5 4h10v5M5 4v16h14v-8M11 15l1-4 7-7 3 3-7 7-4 1Z",
  book: "M4 4h6l2 2 2-2h6v15h-6l-2 2-2-2H4V4ZM12 6v15",
  people:
    "M8 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM2 21v-3a6 6 0 0 1 12 0v3M17 4a4 4 0 0 1 0 8M18 15a5 5 0 0 1 4 5",
  spark: "m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3 3-7Z",
  idea: "M9 18h6M9 22h6M8 15a7 7 0 1 1 8 0l-1 3H9l-1-3Z",
  map: "m3 5 6-3 6 3 6-3v17l-6 3-6-3-6 3V5ZM9 2v17M15 5v17",
  shield: "m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6l9-4ZM8 12l3 3 5-6",
  tune: "M4 3v18M12 3v18M20 3v18M1 8h6M9 16h6M17 8h6",
  image: "M3 3h18v18H3V3ZM3 17l6-6 4 4 3-3 5 5M16 7h.01",
  search: "M16 16l6 6M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z",
  plus: "M12 4v16M4 12h16",
  close: "M6 6l12 12M6 18 18 6",
  panel: "M3 4h18v16H3V4ZM9 4v16",
  trash: "M3 6h18M5 6l1 15h12l1-15M9 6V3h6v3M10 10v7M14 10v7",
  pin: "m8 3 8 0-1 6 4 4H5l4-4-1-6ZM12 13v9",
  clock: "M12 7v5l4 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z",
  arrow: "M5 12h14M13 6l6 6-6 6",
  lock: "M6 10h12v11H6V10ZM8 10V6a4 4 0 0 1 8 0v4",
};
export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] ?? paths.book} />
    </svg>
  );
}
