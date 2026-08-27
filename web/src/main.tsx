import { render } from "solid-js/web";
import App from "./App";
import "./styles.css";
import "./components/components.css";

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root — index.html is malformed.");

render(() => <App />, root);
