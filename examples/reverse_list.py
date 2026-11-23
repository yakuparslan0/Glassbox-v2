"""Reverse a 3-node singly linked list in place with prev / curr / nxt (watch the `next` arrows flip)."""


class Node:
    def __init__(self, val):
        self.val = val
        self.next = None


def reverse(head):
    prev = None
    curr = head
    while curr is not None:
        nxt = curr.next   # remember the rest of the list
        curr.next = prev  # flip the pointer
        prev = curr
        curr = nxt
    return prev


a, b, c = Node(1), Node(2), Node(3)
a.next = b
b.next = c

head = reverse(a)
print(head.val, head.next.val)
