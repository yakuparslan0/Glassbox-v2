"""Binary search tree: insert keys one by one and watch the `left` / `right` branches (and new nodes) appear."""


class TreeNode:
    def __init__(self, val):
        self.val = val
        self.left = None
        self.right = None


def insert(root, val):
    if root is None:
        return TreeNode(val)
    if val < root.val:
        root.left = insert(root.left, val)
    else:
        root.right = insert(root.right, val)
    return root


root = None
for key in [5, 3, 8, 1, 4]:
    root = insert(root, key)
print(root.val, root.left.val, root.right.val)
