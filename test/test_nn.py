import math
import torch
import random
import unittest
from copy import deepcopy
from itertools import repeat

import torch.nn as nn
from torch.autograd import Variable
from common_nn import NNTestCase, ModuleTest, CriterionTest, TestBase, \
    module_tests, criterion_tests, TEST_CUDA, PRECISION


class InputVariableMixin(object):
    def _get_input(self):
        input = TestBase._get_input(self)
        def map_variables(i):
            if isinstance(i, Variable):
                return i
            elif torch.isTensor(i):
                return Variable(i)
            else:
                return type(i)(map_variables(elem) for elem in i)
        return map_variables(input)


class NewModuleTest(InputVariableMixin, ModuleTest):
    def __init__(self, *args, **kwargs):
        super(NewModuleTest, self).__init__(*args, **kwargs)
        self.check_inplace = kwargs.get('check_inplace', False)

    def _do_test(self, test_case, module, input):
        test_case.check_jacobian(module, input, self.jacobian_input)

        if self.check_inplace:
            module_ip = self.constructor(*self.constructor_args, inplace=True)

            output = module(input)
            test_case.assertFalse(input.dirty)

            output2 = module_ip(input)
            test_case.assertTrue(input.dirty)

            test_case.assertEqual(output, output2)


class NewCriterionTest(InputVariableMixin, CriterionTest):
    # TODO: check that criterions don't ignore grad_output

    def _get_target(self, target):
        return Variable(target, requires_grad=False)


class TestNN(NNTestCase):

    def _forward(self, module, input):
        return module(input)

    def _backward(self, module, input, output, grad_output):
        output.backward(grad_output)
        return input.grad

    def _forward_criterion(self, criterion, input, target):
        if isinstance(input, tuple):
            args = input + (target,)
            output = criterion(*args)
        else:
            output = criterion(input, target)
        return output.data[0]

    def _backward_criterion(self, criterion, input, target):
        input_tuple = input if isinstance(input, tuple) else (input,)
        for i in input_tuple:
            i.grad.zero_()
        args = input_tuple + (target,)
        criterion(*args).backward()
        if isinstance(input, tuple):
            return tuple(map(lambda i: i.grad, input))
        else:
            return input.grad

    def _zero_grad_parameters(self, module):
        if hasattr(module, 'weight') and module.weight is not None:
            module.weight.grad.zero_()
        if hasattr(module, 'bias') and module.bias is not None:
            module.bias.grad.zero_()

    def _get_parameters(self, module):
        params = []
        d_params = []
        if hasattr(module, 'weight') and module.weight is not None:
            params += [module.weight.data]
            d_params += [module.weight.grad]
        if hasattr(module, 'bias') and module.bias is not None:
            params += [module.bias.data]
            d_params += [module.bias.grad]
        return params, d_params

    def test_hooks(self):
        module = nn.Sigmoid()
        input = Variable(torch.ones(5, 5))

        counter = {
            'forwards': 0,
            'backwards': 0
        }

        def fw_hook(inc, h_module, input, output):
            self.assertIsInstance(input, tuple)
            self.assertIsInstance(output, Variable)
            self.assertTrue(h_module is module)
            self.assertEqual(input[0].data, torch.ones(5, 5))
            self.assertEqual(output.data, torch.Tensor(5, 5).fill_(1 / (1 + 1 / math.e)))
            counter['forwards'] += inc

        def bw_hook(inc, h_module, grad_input, grad_output):
            self.assertIsInstance(grad_input, tuple)
            self.assertIsInstance(grad_output, tuple)
            self.assertTrue(h_module is module)
            self.assertEqual(grad_output[0], torch.ones(5, 5) * 2)
            counter['backwards'] += inc

        module.register_forward_hook('test', lambda *args: fw_hook(1, *args))

        module(input)
        module(input)
        self.assertEqual(counter['forwards'], 2)
        self.assertEqual(counter['backwards'], 0)

        module.register_backward_hook('test', lambda *args: bw_hook(1, *args))

        output = module(input)
        self.assertEqual(counter['forwards'], 3)
        self.assertEqual(counter['backwards'], 0)

        output.backward(torch.ones(5, 5) * 2)
        self.assertEqual(counter['forwards'], 3)
        self.assertEqual(counter['backwards'], 1)

        output.backward(torch.ones(5, 5) * 2)
        self.assertEqual(counter['forwards'], 3)
        self.assertEqual(counter['backwards'], 2)

        module.register_forward_hook('test2', lambda *args: fw_hook(2, *args))

        output = module(input)
        self.assertEqual(counter['forwards'], 6)
        self.assertEqual(counter['backwards'], 2)

        module.register_backward_hook('test2', lambda *args: bw_hook(2, *args))

        module(input).backward(torch.ones(5, 5) * 2)
        self.assertEqual(counter['forwards'], 9)
        self.assertEqual(counter['backwards'], 5)

        module.remove_backward_hook('test2')

        module(input).backward(torch.ones(5, 5) * 2)
        self.assertEqual(counter['forwards'], 12)
        self.assertEqual(counter['backwards'], 6)

        module.remove_forward_hook('test2')

        module(input).backward(torch.ones(5, 5) * 2)
        self.assertEqual(counter['forwards'], 13)
        self.assertEqual(counter['backwards'], 7)

    def test_volatile(self):
        module = nn.Conv2d(2, 5, kernel_size=3, padding=1)
        input = torch.randn(1, 2, 10, 10)
        x = Variable(input)
        y = Variable(input.clone(), volatile=True)

        output = module(x)
        self.assertFalse(output.volatile)
        self.assertTrue(output.requires_grad)
        output.backward(torch.ones(1, 5, 10, 10))

        vol_output = module(y)
        self.assertTrue(vol_output.volatile)
        self.assertFalse(vol_output.requires_grad)
        self.assertRaises(RuntimeError, lambda: vol_output.backward(torch.ones(1, 5, 10, 10)))

    def _test_dropout(self, cls, input):
        p = 0.2
        input.fill_(1-p)

        module = cls(p)
        input_var = Variable(input)
        output = module(input_var)
        self.assertLess(abs(output.data.mean() - (1-p)), 0.05)
        output.backward(input)
        self.assertLess(abs(input_var.grad.mean() - (1-p)), 0.05)

        module = cls(p, True)
        input_var = Variable(input.clone())
        output = module(input_var + 0)
        self.assertLess(abs(output.data.mean() - (1-p)), 0.05)
        output.backward(input)
        self.assertLess(abs(input_var.grad.mean() - (1-p)), 0.05)

        # Check that these don't raise errors
        module.__repr__()
        str(module)


    def test_Dropout(self):
        input = torch.Tensor(1000)
        self._test_dropout(nn.Dropout, input)

    def test_Dropout2d(self):
        b = random.randint(1, 5)
        w = random.randint(1, 5)
        h = random.randint(1, 5)
        num_features = 1000
        input = torch.Tensor(num_features, b, w, h)
        self._test_dropout(nn.Dropout2d, input)

    def test_Dropout3d(self):
        b = random.randint(1, 5)
        w = random.randint(1, 5)
        h = random.randint(1, 5)
        d = random.randint(1, 2)
        num_features = 1000
        input = torch.Tensor(num_features, b, d, w, h)
        self._test_dropout(nn.Dropout3d, input)

    def _test_maxpool_indices(self, num_dim):
        def expected_indices(dim):
            if dim == 1:
                return torch.DoubleTensor([1, 3])
            lower_dim = expected_indices(dim-1)
            lower_dim = lower_dim.view(1, *lower_dim.size())
            return torch.cat((lower_dim+4, lower_dim+12), 0)

        def expected_grad(dim):
            if dim == 1:
                return torch.DoubleTensor([0, 1, 0, 1])
            lower_dim_grad = expected_grad(dim-1)
            grad = lower_dim_grad.view(1, *lower_dim_grad.size())
            zero = torch.zeros(grad.size())
            return torch.cat((zero, grad, zero, grad), 0)

        module_cls = getattr(nn, 'MaxPool{}d'.format(num_dim))
        module = module_cls(2, return_indices=True)
        numel = 4 ** num_dim
        input = torch.range(1, numel).view(1, 1, *repeat(4, num_dim))
        input_var = Variable(input)

        # Check forward
        output, indices = module(input_var)
        if num_dim != 3:
            expected_indices = expected_indices(num_dim)
            expected_output = expected_indices + 1
            self.assertEqual(indices.data.squeeze(), expected_indices)
            self.assertEqual(output.data.squeeze(), expected_output)
        self.assertTrue(output.requires_grad)
        self.assertFalse(indices.requires_grad)

        # Make sure backward works
        grad_output = torch.DoubleTensor(output.size()).fill_(1)
        output.backward(grad_output)
        expected_grad = expected_grad(num_dim)
        self.assertEqual(input_var.grad, expected_grad.viewAs(input))

        # Make sure backward after changing indices will result in an error
        indices.add_(1)
        self.assertRaises(RuntimeError, lambda: output.backward(grad_output))

    def test_MaxPool1d_indices(self):
        self._test_maxpool_indices(1)

    def test_MaxPool2d_indices(self):
        self._test_maxpool_indices(2)

    def test_MaxPool3d_indices(self):
        self._test_maxpool_indices(3)


def add_test(test):
    test_name = test.get_name()
    cuda_test_name = test_name + '_cuda'
    if hasattr(TestNN, test_name):
        raise RuntimeError('Found two tests with the same name: ' + test_name)
    if hasattr(TestNN, cuda_test_name):
        raise RuntimeError('Found two tests with the same name: ' + cuda_test_name)
    setattr(TestNN, test_name, lambda self,test=test: test(self))
    setattr(TestNN, cuda_test_name, lambda self,test=test: test.test_cuda(self))


new_module_tests = [
    dict(
        module_name='Conv1d',
        constructor_args=(4, 5, 3),
        input_size=(2, 4, 10)
    ),
    dict(
        module_name='Conv1d',
        constructor_args=(4, 5, 3),
        input_size=(2, 4, 10),
        desc='stride'
    ),
    dict(
        module_name='MaxPool1d',
        constructor_args=(4,),
        input_size=(2, 10, 4)
    ),
    dict(
        module_name='MaxPool1d',
        constructor_args=(4, 4),
        input_size=(2, 10, 4),
        desc='stride'
    ),
    dict(
        module_name='Conv2d',
        constructor_args=(3, 4, (3, 3)),
        input_size=(2, 3, 6, 6)
    ),
    dict(
        module_name='Conv2d',
        constructor_args=(3, 4, (3, 3), (2, 2)),
        input_size=(2, 3, 6, 6),
        desc='strided'
    ),
    dict(
        module_name='Conv2d',
        constructor_args=(3, 4, (3, 3), (2, 2), (1, 1)),
        input_size=(2, 3, 6, 6),
        desc='padding'
    ),
    dict(
        module_name='Conv2d',
        constructor_args=(3, 2, (3, 3), (2, 2), (1, 1), (2, 2)),
        input_size=(2, 3, 8, 8),
        desc='dilated'
    ),
    dict(
        module_name='MaxPool2d',
        constructor_args=((3, 3), (2, 2), (1, 1)),
        input_size=(1, 3, 7, 7)
    ),
    dict(
        module_name='AvgPool2d',
        constructor_args=((2, 2),),
        input_size=(2, 3, 6, 6),
    ),
    dict(
        module_name='AvgPool2d',
        constructor_args=((2, 2), (2, 2)),
        input_size=(2, 3, 6, 6),
        desc='stride',
    ),
    dict(
        module_name='AvgPool2d',
        constructor_args=((2, 2), (2, 2), (1, 1)),
        input_size=(2, 3, 6, 6),
        desc='stride_pad',
    ),
    dict(
        module_name='LPPool2d',
        constructor_args=(2, (2, 2), 2),
        input_size=(1, 3, 7, 7)
    ),
    dict(
        module_name='LPPool2d',
        constructor_args=(1.5, 2),
        input=torch.rand(1, 3, 7, 7),
        desc='norm'
    ),
    dict(
        module_name='ReflectionPad2d',
        constructor_args=((1, 2, 3, 4),),
        input_size=(2, 3, 8, 8)
    ),
    dict(
        module_name='ReplicationPad2d',
        constructor_args=((1, 2, 3, 4),),
        input_size=(2, 3, 4, 4)
    ),
    dict(
        module_name='Conv3d',
        constructor_args=(3, 4, 2),
        input_size=(2, 3, 3, 3, 3)
    ),
    dict(
        module_name='Conv3d',
        constructor_args=(3, 4, 2, 2),
        input_size=(2, 3, 5, 5, 5),
        desc='stride'
    ),
    dict(
        module_name='Conv3d',
        constructor_args=(3, 4, 2, 2, 1),
        input_size=(2, 3, 5, 5, 5),
        desc='stride_padding'
    ),
    dict(
        module_name='FullConv3d',
        constructor_args=(2, 3, (2, 2, 2)),
        input_size=(1, 2, 4, 4, 4)
    ),
    dict(
        module_name='MaxPool3d',
        constructor_args=((2, 2, 2),),
        input_size=(2, 3, 5, 5, 5)
    ),
    dict(
        module_name='MaxPool3d',
        constructor_args=(2, (2, 2, 2)),
        input_size=(2, 3, 5, 5, 5),
        desc='stride'
    ),
    dict(
        module_name='MaxPool3d',
        constructor_args=(2, 2, (1, 1, 1)),
        input_size=(2, 3, 5, 5, 5),
        desc='stride_padding'
    ),
    dict(
        module_name='AvgPool3d',
        constructor_args=((2, 2, 2),),
        input_size=(2, 3, 4, 4, 4)
    ),
    dict(
        module_name='AvgPool3d',
        constructor_args=(2, (2, 2, 2)),
        input_size=(2, 3, 5, 5, 5),
        desc='stride'
    ),
    dict(
        module_name='ReplicationPad3d',
        constructor_args=((1, 2, 3, 4, 5, 6),),
        input_size=(2, 3, 5, 5, 5)
    ),
    dict(
        module_name='Embedding',
        constructor_args=(4, 3),
        input=Variable(
            torch.randperm(2).repeatTensor(1, 2).long(),
            requires_grad=False
        ),
        jacobian_input=False
    ),
    dict(
        constructor=lambda: nn.FractionalMaxPool2d(2, output_ratio=0.5, _random_samples=torch.DoubleTensor(1, 3, 2).uniform_()),
        input_size=(1, 3, 5, 5),
        fullname='FractionalMaxPool2d_ratio',
        test_cuda=False),
    dict(
        constructor=lambda: nn.FractionalMaxPool2d((2, 2), output_size=(4, 4), _random_samples=torch.DoubleTensor(1, 3, 2).uniform_()),
        input_size=(1, 3, 7, 7),
        fullname='FractionalMaxPool2d_size',
        test_cuda=False
    ),
]


for test_params in module_tests + new_module_tests:
    # TODO: CUDA is not implemented yet
    if 'constructor' not in test_params:
        name = test_params.pop('module_name')
        test_params['constructor'] = getattr(nn, name)
    test = NewModuleTest(**test_params)
    add_test(test)
for test_params in criterion_tests:
    name = test_params.pop('module_name')
    test_params['constructor'] = getattr(nn, name)
    test = NewCriterionTest(**test_params)
    add_test(test)


class UnpoolingNet2d(nn.Container):

    def __init__(self):
        super(UnpoolingNet2d, self).__init__(
            pool=nn.MaxPool2d(2, return_indices=True),
            unpool=nn.MaxUnpool2d(2)
        )

    def forward(self, input):
        return self.unpool(*self.pool(input))


class UnpoolingNet3d(nn.Container):

    def __init__(self):
        super(UnpoolingNet3d, self).__init__(
            pool=nn.MaxPool3d(2, return_indices=True),
            unpool=nn.MaxUnpool3d(2)
        )

    def forward(self, input):
        return self.unpool(*self.pool(input))


add_test(NewModuleTest(
    constructor=UnpoolingNet2d,
    input_size=(1, 1, 8, 8),
    fullname='MaxUnpool2d_net'))
add_test(NewModuleTest(
    constructor=UnpoolingNet3d,
    input_size=(1, 1, 8, 8, 8),
    fullname='MaxUnpool3d_net'))


if __name__ == '__main__':
    unittest.main()