import { redirect } from "next/navigation";

export default function Home() {
  // Default redirect; the middleware handles auth.
  redirect("/admin/upload");
}
