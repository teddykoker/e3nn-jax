from functools import partial

import haiku as hk
import jax
import jax.numpy as jnp
from e3nn_jax import Irreps, IrrepsData, FunctionalTensorProduct, index_add, Linear, FullyConnectedTensorProduct
from e3nn_jax.nn import HMLP


class Convolution(hk.Module):
    r"""equivariant convolution

    Parameters
    ----------
    irreps_node_input : `e3nn.o3.Irreps`
        representation of the input node features

    irreps_node_attr : `e3nn.o3.Irreps`
        representation of the node attributes

    irreps_edge_attr : `e3nn.o3.Irreps`
        representation of the edge attributes

    irreps_node_output : `e3nn.o3.Irreps` or None
        representation of the output node features

    fc_neurons : list of int
        number of neurons per layers in the fully connected network
        first layer and hidden layers but not the output layer

    num_neighbors : float
        typical number of nodes convolved over
    """
    def __init__(self, irreps_node_input, irreps_node_attr, irreps_edge_attr, irreps_node_output, fc_neurons, num_neighbors, mixing_angle=jnp.pi / 8.0):
        super().__init__()

        self.irreps_node_input = Irreps(irreps_node_input)
        self.irreps_node_attr = Irreps(irreps_node_attr) if irreps_node_attr is not None else None
        self.irreps_edge_attr = Irreps(irreps_edge_attr)
        self.irreps_node_output = Irreps(irreps_node_output)
        self.fc_neurons = fc_neurons
        self.num_neighbors = num_neighbors
        self.mixing_angle = mixing_angle

    @partial(jax.profiler.annotate_function, name="convolution")
    def __call__(self, node_input, edge_src, edge_dst, edge_attr, node_attr=None, edge_scalar_attr=None):
        node_input = IrrepsData.new(self.irreps_node_input, node_input)
        edge_attr = IrrepsData.new(self.irreps_edge_attr, edge_attr)

        # def stat(text, z):
        #     print(f"{text} = {jax.tree_map(lambda x: float(jnp.mean(jnp.mean(x**2, axis=1))), z)}")

        if self.irreps_node_attr is not None and node_attr is not None:
            node_attr = IrrepsData.new(self.irreps_node_attr, node_attr)

            tmp = jax.vmap(FullyConnectedTensorProduct(self.irreps_node_input + self.irreps_node_output))(node_input, node_attr)
        else:
            tmp = jax.vmap(Linear(self.irreps_node_input + self.irreps_node_output))(node_input)

        node_features, node_self_out = tmp.list[:len(self.irreps_node_input)], tmp.list[len(self.irreps_node_input):]

        # stat('node_features', node_features)
        # stat('node_self_out', node_self_out)

        edge_features = jax.tree_map(lambda x: x[edge_src], node_features)

        # stat('edge_features', edge_features)
        ######################################################################################

        irreps_mid = []
        instructions = []
        for i, (mul, ir_in) in enumerate(self.irreps_node_input):
            for j, (_, ir_edge) in enumerate(self.irreps_edge_attr):
                for ir_out in ir_in * ir_edge:
                    if ir_out in self.irreps_node_output or ir_out.is_scalar():
                        k = len(irreps_mid)
                        irreps_mid.append((mul, ir_out))
                        instructions.append((i, j, k, 'uvu', True))
        irreps_mid = Irreps(irreps_mid)

        assert irreps_mid.dim > 0, (
            f"irreps_node_input={self.irreps_node_input} "
            f"time irreps_edge_attr={self.irreps_edge_attr} "
            f"produces nothing in irreps_node_output={self.irreps_node_output}"
        )

        irreps_mid, p, _ = irreps_mid.sort()
        instructions = [
            (i_1, i_2, p[i_out], mode, train)
            for i_1, i_2, i_out, mode, train in instructions
        ]

        tp = FunctionalTensorProduct(
            self.irreps_node_input,
            self.irreps_edge_attr,
            irreps_mid,
            instructions,
        )
        irreps_mid = irreps_mid.simplify()

        if self.fc_neurons:
            weight = HMLP(
                self.fc_neurons,
                jax.nn.gelu
            )(edge_scalar_attr)

            # stat('weight', weight)

            weight = [
                jnp.einsum(
                    "x...,ex->e...",
                    hk.get_parameter(
                        f'weight {ins.i_in1} x {ins.i_in2} -> {ins.i_out}',
                        shape=(weight.shape[1],) + ins.path_shape,
                        init=hk.initializers.RandomNormal()
                    ) / weight.shape[1]**0.5,
                    weight
                )
                for ins in tp.instructions
            ]

            # stat('weight', weight)

            edge_features: IrrepsData = jax.vmap(tp.left_right, (0, 0, 0), 0)(weight, edge_features, edge_attr)
        else:
            weight = [
                hk.get_parameter(
                    f'weight {ins.i_in1} x {ins.i_in2} -> {ins.i_out}',
                    shape=ins.path_shape,
                    init=hk.initializers.RandomNormal()
                )
                for ins in tp.instructions
            ]
            edge_features: IrrepsData = jax.vmap(tp.left_right, (None, 0, 0), 0)(weight, edge_features, edge_attr)

        # stat('edge_features 2', edge_features)

        ######################################################################################

        shape = node_input._shape_from_list()

        node_features = index_add(edge_dst, edge_features.contiguous, out_dim=shape[0])

        node_features = node_features / self.num_neighbors**0.5

        node_features = IrrepsData.from_contiguous(irreps_mid, node_features)
        # stat('node_features', node_features)

        ######################################################################################

        if self.irreps_node_attr is not None and node_attr is not None:
            node_conv_out = jax.vmap(FullyConnectedTensorProduct(self.irreps_node_output))(node_features, node_attr)
        else:
            node_conv_out = jax.vmap(Linear(self.irreps_node_output))(node_features)

        # stat('node_conv_out', node_conv_out)

        ######################################################################################

        with jax.ensure_compile_time_eval():
            c = jnp.cos(self.mixing_angle)
            s = jnp.sin(self.mixing_angle)

        def f(x, y):
            if x is None and y is None:
                return None
            if x is None:
                return y
            if y is None:
                return x
            return c * x + s * y

        output = [f(x, y) for x, y in zip(node_self_out, node_conv_out.list)]

        return IrrepsData.from_list(self.irreps_node_output, output)
