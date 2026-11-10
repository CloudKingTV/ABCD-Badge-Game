-- How to play, and this badge's id (an ABCD member's goes in data/members.txt).

ui.label(8, 4, "How Kinlings works", {size = 14, color = "green"})
ui.label(8, 28, "Your creature only eats what other people give it.", {size = 12, w = 304})
ui.label(8, 62, "Walk around to forage snacks. SELECT swaps one with someone nearby who has Kinlings open.", {size = 12, w = 304})
ui.label(8, 112, "Each new person who feeds it helps it grow, and joins your pack.", {size = 12, w = 304})
ui.label(8, 146, "ABCD members give it something to wear: LEFT and RIGHT change it.", {size = 12, w = 304})
ui.label(8, 184, "Your badge: " .. ME, {size = 12, color = "dim"})
ui.hint("CANCEL back")
