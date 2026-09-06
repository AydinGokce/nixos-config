import { createRoot } from "react-dom/client";
import App, { AppBoundary } from "./App";
import "./styles.css";
createRoot(document.getElementById("root")!).render(
  <AppBoundary>
    <App />
  </AppBoundary>,
);
