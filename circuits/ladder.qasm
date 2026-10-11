OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
h q[0];
cx q[0],q[1];
rz(0.37) q[1];
cx q[1],q[2];
rz(0.61) q[2];
cx q[0],q[2];
// Synthetic review acceptance: circuit semantics unchanged.
// Synthetic prompt v2 acceptance: same routing regression.
