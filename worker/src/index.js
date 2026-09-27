// Entry point of the golf-caddie Worker. The logic is in relay.js (read its header for what it enforces).
// Only the default export is allowed here: workerd treats any named export of this module as an entrypoint.
import { respond } from "./relay.js";

export default { fetch: respond };
