"""Build a list whose tail points back into the middle, then catch the cycle with Floyd's slow/fast pointers."""


class Node:
    def __init__(self, val):
        self.val = val
        self.next = None


def has_cycle(head):
    slow = head
    fast = head
    while fast is not None and fast.next is not None:
        slow = slow.next
        fast = fast.next.next
        if slow is fast:
            return True
    return False


a, b, c, d = Node(1), Node(2), Node(3), Node(4)
a.next = b
b.next = c
c.next = d
print(has_cycle(a))

d.next = b  # tail -> 2 closes the cycle
print(has_cycle(a))
