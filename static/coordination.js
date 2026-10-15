/* Pure view reducer. Mirrored and proved in formal/coordination.
 * Callers supply exact run/message identity; text and arrival time do not.
 * This is loaded before chat.js and tested in Node and Chromium.
 */
(function (root) {
    'use strict';
    function viewStep(landed, sameIdentity, incomingLanded) {
        if (!sameIdentity || (landed && !incomingLanded)) return [landed, false];
        return [incomingLanded, true];
    }
    function launchStep(state, event) {
        switch (event) {
            case 'submit': return state === 'idle' ? 'submitting' : state;
            case 'admitted': return ['submitting', 'reconciling'].includes(state) ? 'running' : state;
            case 'disconnected': return ['submitting', 'running'].includes(state) ? 'reconciling' : state;
            case 'terminal': return state === 'idle' ? state : 'landed';
            case 'rejected': return state === 'idle' ? state : 'failed';
            default: throw new Error('Unknown launch transition');
        }
    }
    const api = Object.freeze({ viewStep, launchStep });
    root.LoomCoordination = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
